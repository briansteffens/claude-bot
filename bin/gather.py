#!/usr/bin/env python3
"""Print a JSON snapshot of every configured project's claude-bot issues.

/tick runs this at the start of every pass and treats its output as the
authoritative issue list, so newly labelled issues can't be missed. It reads
bot.config.yml and talks to GitHub through `gh api` (REST only: cloud sessions
block GraphQL).

Usage: bin/gather.py [path/to/bot.config.yml]
"""
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

STATUS_MARKER = "<!-- claude-bot:status"
BOT_MARKER = "<!-- claude-bot"
ACTIVE_PHASES = {"dispatching", "designing", "needs-input", "approved", "building", "in-review", "blocked"}


ETAG_CACHE_PATH = os.path.expanduser("~/.cache/claude-bot/etags.json")
_etags = None


def _etag_cache():
    global _etags
    if _etags is None:
        try:
            _etags = json.load(open(ETAG_CACHE_PATH))
        except (OSError, ValueError):
            _etags = {}
    return _etags


def save_etag_cache():
    if _etags is not None:
        os.makedirs(os.path.dirname(ETAG_CACHE_PATH), exist_ok=True)
        json.dump(_etags, open(ETAG_CACHE_PATH, "w"))


def gh_paginated(path):
    # Page by number on the same repos/{owner}/{repo} path: GitHub's Link headers
    # (which `gh --paginate` follows) use repositories/{id}/..., which the cloud
    # proxy refuses.
    per_page = int(re.search(r"[?&]per_page=(\d+)", path).group(1)) if "per_page=" in path else 30
    items = []
    for page in range(1, 51):
        sep = "&" if "?" in path else "?"
        result = subprocess.run(["gh", "api", f"{path}{sep}page={page}"], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"gh api {path}: {result.stderr.strip()}")
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            return data
        items.extend(data)
        if len(data) < per_page:
            break
    return items


def gh(path):
    """GET a REST path. Uses a conditional request when we've seen it before, so
    unchanged data comes back as a cheap 304 (this runs every minute)."""
    cache = _etag_cache()
    cached = cache.get(path)
    command = ["gh", "api", "-i", path]
    if cached:
        command[3:3] = ["-H", f"If-None-Match: {cached['etag']}"]
    result = subprocess.run(command, capture_output=True, text=True)
    head, _, body = result.stdout.replace("\r\n", "\n").partition("\n\n")
    status_line = head.split("\n", 1)[0]
    if " 304" in status_line and cached:
        return cached["body"]
    if result.returncode != 0 or " 200" not in status_line:
        raise RuntimeError(f"gh api {path}: {result.stderr.strip() or status_line}")
    headers = {}
    for line in head.split("\n")[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    if 'rel="next"' in headers.get("link", ""):
        # More than one page: rare here, so skip caching and fetch them all.
        return gh_paginated(path)
    data = json.loads(body)
    if headers.get("etag"):
        cache[path] = {"etag": headers["etag"], "body": data}
    return data


def load_config(path):
    text = open(path).read()
    try:
        import yaml
        config = yaml.safe_load(text)
        repos = [p["repo"] for p in config.get("projects") or []]
        owners = config.get("owners") or []
        merged = config.get("dashboard_merged", 25)
    except ImportError:
        repos = re.findall(r"^\s*-\s*repo:\s*(\S+)", text, re.M)
        owners = re.findall(r"^\s*-\s*(\S+)\s*$", text.split("owners:")[1].split("\n\n")[0], re.M)
        found = re.search(r"^dashboard_merged:\s*(\d+)", text, re.M)
        merged = int(found.group(1)) if found else 25
    return repos, set(owners), merged


def parse_status(comments):
    for comment in comments:
        body = comment.get("body") or ""
        if body.startswith(STATUS_MARKER):
            match = re.match(r"<!-- claude-bot:status (\{.*?\}) -->", body, re.S)
            try:
                status = json.loads(match.group(1)) if match else {}
            except json.JSONDecodeError as error:
                status = {"_parse_error": str(error)}
            return comment["id"], status
    return None, None


def phase_label(labels):
    phases = [name[len("claude:"):] for name in labels if name.startswith("claude:")]
    return [p for p in phases if p not in ("next", "paused", "ping")]


def infer_review_state(repo, pr_number, owners):
    """For workers that predate review_state: whose turn is it on this PR?"""
    conversation = gh(f"repos/{repo}/issues/{pr_number}/comments?per_page=100")
    reviews = gh(f"repos/{repo}/pulls/{pr_number}/reviews?per_page=100")
    commits = gh(f"repos/{repo}/pulls/{pr_number}/commits?per_page=100")
    owner_times = [c["created_at"] for c in conversation
                   if c["user"]["login"] in owners and not (c.get("body") or "").startswith(BOT_MARKER)]
    owner_times += [r["submitted_at"] for r in reviews
                    if r["user"]["login"] in owners and (r.get("body") or "").strip() and r.get("submitted_at")]
    bot_comments = [c for c in conversation if (c.get("body") or "").startswith(BOT_MARKER)]
    bot_times = [c["created_at"] for c in bot_comments]
    commit_times = [c["commit"]["committer"]["date"] for c in commits]
    latest_owner = max(owner_times, default="")
    latest_bot = max(bot_times + commit_times, default="")
    revisions = [int(n) for c in bot_comments for n in re.findall(r"Revision (\d+) ready", c.get("body") or "")]
    return {
        "inferred_review_state": "revising" if latest_owner > latest_bot else "ready",
        "inferred_revision": max(revisions, default=1),
    }


def describe_issue(repo, issue, owners):
    labels = [label["name"] for label in issue["labels"]]
    comments = gh(f"repos/{repo}/issues/{issue['number']}/comments?per_page=100")
    status_id, status = parse_status(comments)
    handled = (status or {}).get("handled_through") or 0
    owner_comments = [
        {"id": c["id"], "at": c["created_at"]}
        for c in comments
        if c["user"]["login"] in owners
        and not (c.get("body") or "").startswith(BOT_MARKER)
        and c["id"] > handled
    ]
    entry = {
        "number": issue["number"],
        "title": issue["title"],
        "url": issue["html_url"],
        "state": issue["state"],
        "created_at": issue["created_at"],
        "closed_at": issue.get("closed_at"),
        "labels": labels,
        "phase_labels": phase_label(labels),
        "next": "claude:next" in labels,
        "paused": "claude:paused" in labels,
        "ping": "claude:ping" in labels,
        "status_comment_id": status_id,
        "status": status,
        "phase": (status or {}).get("phase") or "queued",
        "unhandled_owner_issue_comments": owner_comments,
    }
    pr_number = (status or {}).get("pr")
    # A PR can live in another repo than its issue (e.g. a factory issue whose
    # change lands in claude-bot); the status JSON then names it as pr_repo.
    pr_repo = (status or {}).get("pr_repo") or repo
    if pr_number:
        pr = gh(f"repos/{pr_repo}/pulls/{pr_number}")
        entry["pr"] = {
            "number": pr_number,
            "url": pr["html_url"],
            "state": pr["state"],
            "draft": pr["draft"],
            "merged": pr.get("merged", False),
            "merged_at": pr.get("merged_at"),
            "head_sha": pr["head"]["sha"],
            "head_ref": pr["head"]["ref"],
        }
        body = pr.get("body") or ""
        heading = body.find("Try it")
        if heading >= 0:
            link = re.search(r"https?://[^\s)>\]]+", body[heading:heading + 600])
            entry["pr"]["try_it_url"] = link.group(0) if link else None
        if entry["phase"] == "in-review" and pr["state"] == "open" and not (status or {}).get("review_state"):
            entry["pr"].update(infer_review_state(pr_repo, pr_number, owners))
    return entry


CACHE_PATH = os.path.expanduser("~/.cache/claude-bot/merged.json")


def load_cache():
    try:
        return json.load(open(CACHE_PATH))
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    json.dump(cache, open(CACHE_PATH, "w"))


def merged_pr_stats(repo, issue_number, owners, cache):
    """PR number and comment counts for a merged issue. Merged PRs don't change,
    so results are cached across runs."""
    key = f"{repo}#{issue_number}"
    if key in cache:
        return cache[key]
    _, status = parse_status(gh(f"repos/{repo}/issues/{issue_number}/comments?per_page=100"))
    pr_number = (status or {}).get("pr")
    pr_repo = (status or {}).get("pr_repo") or repo
    if not pr_number:
        stats = {"pr": None}
    else:
        pr = gh(f"repos/{pr_repo}/pulls/{pr_number}")
        conversation = gh(f"repos/{pr_repo}/issues/{pr_number}/comments?per_page=100")
        inline = gh(f"repos/{pr_repo}/pulls/{pr_number}/comments?per_page=100")
        reviews = [r for r in gh(f"repos/{pr_repo}/pulls/{pr_number}/reviews?per_page=100") if (r.get("body") or "").strip()]
        everything = conversation + inline + reviews

        def from_owner(c):
            return c["user"]["login"] in owners and not (c.get("body") or "").startswith(BOT_MARKER)

        stats = {
            "pr": pr_number,
            "pr_url": pr["html_url"],
            "merged_at": pr.get("merged_at"),
            "comments": len(everything),
            "owner_comments": sum(1 for c in everything if from_owner(c)),
        }
    if stats["pr"] is None or stats.get("merged_at"):
        cache[key] = stats
    return stats


def queue_key(entry):
    return (0 if entry["next"] else 1, entry["number"])


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "bot.config.yml"
    repos, owners, merged_count = load_config(config_path)
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
    snapshot = {"checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "projects": [], "recently_merged": [], "errors": []}

    for repo in repos:
        project = {"repo": repo, "issues": []}
        try:
            issues = gh(f"repos/{repo}/issues?labels=claude&state=open&per_page=100")
            closed = gh(f"repos/{repo}/issues?labels=claude&state=closed&since={week_ago}&per_page=100")
        except RuntimeError as error:
            snapshot["errors"].append(str(error))
            snapshot["projects"].append(project)
            continue
        # The issues endpoint also returns PRs; skip them.
        issues = [i for i in issues if "pull_request" not in i]
        closed = [i for i in closed if "pull_request" not in i]
        # Closed issues still carrying an active phase label: /tick finalizes
        # or cancels these, so they're included too.
        wanted = issues + [i for i in closed
                           if set(phase_label([label["name"] for label in i["labels"]])) & ACTIVE_PHASES]
        with ThreadPoolExecutor(max_workers=8) as pool:
            project["issues"] = list(pool.map(lambda issue: describe_issue(repo, issue, owners), wanted))
        done = gh(f"repos/{repo}/issues?labels=claude:done&state=closed&sort=updated&per_page={merged_count}")
        for issue in done:
            if "pull_request" not in issue:
                snapshot["recently_merged"].append({
                    "repo": repo, "number": issue["number"], "title": issue["title"],
                    "url": issue["html_url"], "closed_at": issue["closed_at"],
                })
        project["issues"].sort(key=queue_key)
        project["counts"] = {}
        for entry in project["issues"]:
            project["counts"][entry["phase"]] = project["counts"].get(entry["phase"], 0) + 1
        snapshot["projects"].append(project)

    # Merged PRs whose issue the bot hasn't finalized yet (still labelled
    # in-review) count as merged too, so they don't vanish from both lists.
    listed = {(m["repo"], m["number"]) for m in snapshot["recently_merged"]}
    for project in snapshot["projects"]:
        for issue in project["issues"]:
            pr = issue.get("pr") or {}
            if pr.get("merged") and (project["repo"], issue["number"]) not in listed:
                snapshot["recently_merged"].append({
                    "repo": project["repo"], "number": issue["number"], "title": issue["title"],
                    "url": issue["url"], "closed_at": issue.get("closed_at") or pr.get("merged_at"),
                })
    snapshot["recently_merged"].sort(key=lambda m: m["closed_at"] or "", reverse=True)
    snapshot["recently_merged"] = snapshot["recently_merged"][:merged_count]
    cache = load_cache()

    def add_stats(merged):
        try:
            merged.update(merged_pr_stats(merged["repo"], merged["number"], owners, cache))
        except RuntimeError as error:
            snapshot["errors"].append(str(error))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add_stats, snapshot["recently_merged"]))
    save_cache(cache)
    save_etag_cache()
    json.dump(snapshot, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
