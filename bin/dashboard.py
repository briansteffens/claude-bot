#!/usr/bin/env python3
"""Render the claude-bot dashboard from a gather.py snapshot, and optionally
write it to the "claude-bot queue" issue.

/tick runs this at the end of every turn, so the dashboard always reflects
GitHub as of that moment rather than whatever the orchestrator remembers.

Usage:
  bin/dashboard.py [snapshot.json] [--write]

With no snapshot path it runs bin/gather.py itself. --write PATCHes the
dashboard issue (found by title in this repo) with the rendered body;
otherwise the markdown is printed.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DASHBOARD_TITLE = "claude-bot queue"
HOLDING = ("building", "in-review")
ORDER = ["in-review", "building", "approved", "needs-input", "designing", "dispatching", "blocked", "queued"]


def load_settings():
    """Per-project settings merged over defaults, plus the global ones."""
    import yaml
    config = yaml.safe_load(open(os.path.join(ROOT, "bot.config.yml")))
    defaults = config.get("defaults") or {}
    projects = {p["repo"]: {**defaults, **p} for p in config.get("projects") or []}
    return config, projects


def zone():
    """The owner's timezone from bot.config.yml (default Pacific)."""
    import yaml
    config = yaml.safe_load(open(os.path.join(ROOT, "bot.config.yml")))
    return ZoneInfo(config.get("timezone") or "America/Los_Angeles")


TZ = zone()


def clock(moment):
    return moment.strftime("%I:%M%p").lstrip("0").lower()


def when(stamp, today):
    if not stamp:
        return ""
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(TZ)
    return clock(moment) if moment.date() == today else f"{moment:%b} {moment.day} {clock(moment)}"


def link(text, url):
    return f"[{text}]({url})" if url else text


def short(repo):
    return repo.split("/", 1)[1]


def review(issue):
    """(state, revision) for an in-review issue, recorded or inferred."""
    status = issue.get("status") or {}
    pr = issue.get("pr") or {}
    state = status.get("review_state") or pr.get("inferred_review_state") or "ready"
    revision = status.get("revision") or pr.get("inferred_revision") or 1
    return state, revision


def preview_of(issue):
    return (issue.get("status") or {}).get("preview") or (issue.get("pr") or {}).get("try_it_url")


def live(issue):
    """Still the bot's business: open, not paused, PR not merged."""
    pr = issue.get("pr") or {}
    return issue["state"] == "open" and not pr.get("merged") and issue["phase"] not in ("done", "cancelled")


def needs_you_rows(snapshot, today):
    rows = []
    for project in snapshot["projects"]:
        for issue in project["issues"]:
            if not live(issue) or issue["paused"]:
                continue
            status = issue.get("status") or {}
            pr = issue.get("pr") or {}
            phase = issue["phase"]
            links = []
            if pr:
                links.append(link(f"PR #{pr['number']}", pr["url"]))
            if phase == "in-review":
                state, revision = review(issue)
                if state != "ready":
                    continue
                action = "▶️ ready to play & merge" if revision <= 1 else f"▶️ ready to test revision {revision}"
                preview = preview_of(issue)
                if preview:
                    links.append(link("**Preview**", preview))
                else:
                    action += " (preview missing)"
            elif phase == "needs-input":
                action = "❓ waiting on your answers"
            elif phase == "blocked":
                action = "⛔ blocked: see the status comment"
            else:
                continue
            since = status.get("waiting_since") or status.get("updated") or issue["created_at"]
            what = link(f"{short(project['repo'])}#{issue['number']}", issue["url"])
            rows.append((since, f"| {when(since, today)} | {what} {issue['title']} | {action} | {' · '.join(links) or '—'} |"))
    rows.sort(key=lambda row: row[0], reverse=True)
    return [row for _, row in rows]


def phase_text(issue):
    phase = issue["phase"]
    status = issue.get("status") or {}
    if phase == "in-review":
        state, revision = review(issue)
        return f"🔧 responding to feedback (rev {revision + 1} coming)" if state == "revising" else "👀 in review"
    if phase == "approved":
        return "✅ approved (fast path)" if status.get("design") == "skipped" else "✅ approved"
    return {
        "queued": "⏳ queued", "dispatching": "🚚 starting", "designing": "✏️ designing",
        "needs-input": "❓ needs input", "building": "🔨 building", "blocked": "⛔ blocked",
    }.get(phase, phase)


def notes_for(issue, slots_full, holders):
    status = issue.get("status") or {}
    if issue["paused"]:
        return "paused"
    if issue["phase"] in HOLDING:
        return "slot"
    if issue["phase"] != "approved":
        return ""
    parallel = status.get("parallel") or {}
    if parallel.get("ok") is False and parallel.get("reason"):
        return f"waiting: {parallel['reason']}"
    # A verdict only means something against what's building now.
    current = [n for n in parallel.get("with") or [] if n in holders]
    if parallel.get("ok") and current:
        safe = ", ".join(f"#{n}" for n in current)
        return f"waiting for a build slot (safe with {safe})" if slots_full else f"next up (safe with {safe})"
    return "waiting for a build slot" if slots_full else "next up"


def project_section(project, settings, today):
    repo = project["repo"]
    issues = [i for i in project["issues"] if live(i)]
    total = settings.get("build_slots", 2)
    holders = {i["number"] for i in issues if i["phase"] in HOLDING}
    used = len(holders)
    if not issues:
        return [f"### {repo}", "idle", ""]
    lines = [f"### {repo} · slots {used}/{total}",
             "| # | Issue | Phase | PR | Preview | Session | Notes |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    ranked = sorted(issues, key=lambda i: ORDER.index(i["phase"]) if i["phase"] in ORDER else len(ORDER))
    for position, issue in enumerate(ranked, 1):
        status = issue.get("status") or {}
        pr = issue.get("pr") or {}
        pr_cell = link(f"#{pr['number']}", pr["url"]) + (" (draft)" if pr.get("draft") else "") if pr else "—"
        preview = preview_of(issue) if issue["phase"] == "in-review" else None
        session = link("open", status.get("session_url")) if status.get("session_url") else "—"
        lines.append(
            f"| {position} | {link('#' + str(issue['number']), issue['url'])} {issue['title']} | {phase_text(issue)} | {pr_cell} "
            f"| {link('▶️', preview) if preview else '—'} | {session} | {notes_for(issue, used >= total, holders)} |"
        )
    lines.append("")
    return lines


def merged_section(snapshot, today):
    lines = ["## Recently merged", "| Merged | What | 💬 Comments | PR |", "| --- | --- | --- | --- |"]
    for merged in snapshot["recently_merged"]:
        if merged.get("comments") is None:
            comments = "—"
        else:
            comments = str(merged["comments"]) + (f" ({merged['owner_comments']} yours)" if merged.get("owner_comments") else "")
        links = link(f"PR #{merged['pr']}", merged.get("pr_url")) if merged.get("pr") else "—"
        stamp = merged.get("merged_at") or merged["closed_at"]
        what = link(f"{short(merged['repo'])}#{merged['number']}", merged["url"])
        lines.append(f"| {when(stamp, today)} | {what} {merged['title']} | {comments} | {links} |")
    return lines


def render(snapshot):
    _, projects = load_settings()
    checked = datetime.fromisoformat(snapshot["checked_at"].replace("Z", "+00:00")).astimezone(TZ)
    today = checked.date()
    checked_text = f"{checked:%b} {checked.day} {clock(checked)} {checked:%Z}"
    lines = ["<!-- claude-bot -->", "## 🔔 Needs you"]
    rows = needs_you_rows(snapshot, today)
    if rows:
        lines += ["| Since | What | Action | Links |", "| --- | --- | --- | --- |"] + rows
        lines += ["", f"_Nothing else is waiting on you._ · Checked {checked_text}"]
    else:
        lines += [f"_Nothing needs you right now._ · Checked {checked_text}"]
    if snapshot.get("errors"):
        lines += ["", "> ⚠️ Some data couldn't be fetched this pass: " + "; ".join(snapshot["errors"])[:500]]
    lines += ["", "## In progress", ""]
    for project in snapshot["projects"]:
        lines += project_section(project, projects.get(project["repo"], {}), today)
    lines += merged_section(snapshot, today)
    return "\n".join(lines) + "\n"


def gh(*args, input_text=None):
    result = subprocess.run(["gh", "api", *args], input=input_text, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)}: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def this_repo():
    url = subprocess.run(["git", "-C", ROOT, "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    match = re.search(r"github\.com[/:]([^/]+/[^/.]+)", url)
    if not match:
        raise RuntimeError(f"can't tell this repo's GitHub name from origin {url!r}")
    return match.group(1)


def write(body):
    repo = this_repo()
    issues = gh(f"repos/{repo}/issues?state=open&per_page=100")
    found = [i for i in issues if i["title"] == DASHBOARD_TITLE and "pull_request" not in i]
    payload = json.dumps({"body": body})
    if found:
        number = found[0]["number"]
        # The proxy appends an attribution footer to bodies it writes; ignore it.
        current = (found[0].get("body") or "").split("\n---\n_Generated by")[0].strip()
        if current == body.strip():
            return f"unchanged #{number}"
        gh("-X", "PATCH", f"repos/{repo}/issues/{number}", "--input", "-", input_text=payload)
        return f"updated #{number}"
    created = gh("-X", "POST", f"repos/{repo}/issues", "--input", "-",
                 input_text=json.dumps({"title": DASHBOARD_TITLE, "body": body}))
    return f"created #{created['number']}"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        snapshot = json.load(open(args[0]))
    else:
        out = subprocess.run([os.path.join(HERE, "gather.py"), os.path.join(ROOT, "bot.config.yml")],
                             capture_output=True, text=True, check=True).stdout
        snapshot = json.loads(out)
    body = render(snapshot)
    if "--write" in sys.argv:
        print(write(body))
    else:
        sys.stdout.write(body)


if __name__ == "__main__":
    main()
