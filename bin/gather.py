#!/usr/bin/env python3
"""Print a JSON snapshot of every configured project's claude-bot issues.

/tick runs this at the start of every pass and treats its output as the
authoritative issue list, so newly labelled issues can't be missed. It reads
bot.config.yml and talks to GitHub through `gh api` (REST only: cloud sessions
block GraphQL).

Usage: bin/gather.py [path/to/bot.config.yml]
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

STATUS_MARKER = "<!-- claude-bot:status"
BOT_MARKER = "<!-- claude-bot"
ACTIVE_PHASES = {"dispatching", "designing", "needs-input", "approved", "building", "in-review", "blocked"}


def gh(path):
    """GET a REST path, following pagination; returns a list for list endpoints."""
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {path}: {result.stderr.strip()}")
    pages = json.loads(result.stdout)
    if pages and isinstance(pages[0], list):
        return [item for page in pages for item in page]
    return pages[0] if len(pages) == 1 else pages


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
    return [p for p in phases if p not in ("next", "paused")]


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
        "status_comment_id": status_id,
        "status": status,
        "phase": (status or {}).get("phase") or "queued",
        "unhandled_owner_issue_comments": owner_comments,
    }
    pr_number = (status or {}).get("pr")
    if pr_number:
        pr = gh(f"repos/{repo}/pulls/{pr_number}")
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
    return entry


def queue_key(entry):
    return (0 if entry["next"] else 1, entry["number"])


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "bot.config.yml"
    repos, owners, merged_count = load_config(config_path)
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        for issue in issues:
            project["issues"].append(describe_issue(repo, issue, owners))
        for issue in closed:
            labels = [label["name"] for label in issue["labels"]]
            if set(phase_label(labels)) & ACTIVE_PHASES:
                # Closed while still active: /tick finalizes or cancels these.
                project["issues"].append(describe_issue(repo, issue, owners))
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

    snapshot["recently_merged"].sort(key=lambda m: m["closed_at"], reverse=True)
    snapshot["recently_merged"] = snapshot["recently_merged"][:merged_count]
    json.dump(snapshot, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
