#!/usr/bin/env python3
"""Wake the orchestrator as soon as a new issue is queued.

Polls GitHub every WATCH_INTERVAL seconds (default 30) for open issues
labelled `claude` that carry no phase label yet (no `claude:<phase>` besides
next/paused/ping) and aren't paused, i.e. issues nobody has dispatched. Exits
as soon as it finds one, printing them, so that a harness that runs it as a
background task re-invokes the orchestrator right away. The orchestrator then
runs /tick (which dispatches them) and starts the watcher again.

One cheap REST call per project per pass. Exits 0 with output on a hit, and
exits 0 silently after WATCH_MAX seconds (default 3300) so a stale watcher
never outlives the session's interest in it.

    bin/watch-new.py          # ignore issues already queued at start
    bin/watch-new.py --all    # report those too
"""
import json
import os
import subprocess
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
QUIET = {"claude", "claude:next", "claude:paused", "claude:ping"}


def projects():
    with open(os.path.join(ROOT, "bot.config.yml")) as handle:
        config = yaml.safe_load(handle)
    return [p["repo"] for p in config.get("projects", [])]


def queued(repo):
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues?labels=claude&state=open&per_page=100"],
        capture_output=True, text=True)
    if result.returncode != 0:
        return []
    found = []
    for issue in json.loads(result.stdout):
        if "pull_request" in issue:
            continue
        labels = {label["name"] for label in issue["labels"]}
        if "claude:paused" in labels or labels - QUIET:
            continue
        # A ping on a queued issue means "start it now": report it as new even
        # if it was already queued when the watcher started.
        tag = " (ping)" if "claude:ping" in labels else ""
        found.append(f"{repo}#{issue['number']} {issue['title']}{tag}")
    return found


def main():
    interval = int(os.environ.get("WATCH_INTERVAL", "30"))
    deadline = time.time() + int(os.environ.get("WATCH_MAX", "3300"))
    # Issues already queued when the watcher starts were seen by the tick that
    # started it (left queued on purpose, e.g. design_ahead is full): only
    # report ones that show up after that.
    known = set() if "--all" in sys.argv else {hit for repo in projects() for hit in queued(repo)}
    while time.time() < deadline:
        hits = [hit for repo in projects() for hit in queued(repo) if hit not in known]
        if hits:
            print("new queued issue(s), run /tick:")
            print("\n".join(hits))
            return
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
