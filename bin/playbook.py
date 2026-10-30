#!/usr/bin/env python3
"""Print the current worker playbook for create_session's append_system_prompt.

The playbook changes often, so the orchestrator must pass this script's output
verbatim on every dispatch rather than a copy it read earlier. The output ends
with a version stamp that workers record in their status JSON, so the dashboard
can flag workers still running an older playbook.

Usage:
  bin/playbook.py            the playbook, front-matter removed, version stamped
  bin/playbook.py --version  just the version
"""
import hashlib
import os
import sys

SKILL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".claude", "skills", "work-issue", "SKILL.md")


def body():
    text = open(SKILL).read()
    if text.startswith("---"):
        text = text.split("\n---\n", 1)[1]
    return text.strip() + "\n"


def version():
    return hashlib.sha1(body().encode()).hexdigest()[:10]


def main():
    if "--version" in sys.argv:
        print(version())
        return
    sys.stdout.write(body())
    print(f"\nPlaybook version: {version()} (record it as `playbook` in your status JSON).")


if __name__ == "__main__":
    main()
