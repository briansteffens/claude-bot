#!/usr/bin/env python3
"""Do the GitHub side of dispatching a worker, and prepare create_session's arguments.

Scripts can't call create_session (it's an MCP tool of the orchestrator
session), so dispatch is three steps:

  bin/dispatch.py prepare o/r#n --orchestrator <session id> [options]
      -> posts/updates the status comment (phase dispatching, handled_through,
         run) and the claude:designing label; prints create_session's arguments
         as JSON (also saved to ~/.cache/claude-bot/dispatch/<owner>-<repo>-<n>.json)
  create_session(**those arguments)            (the orchestrator)
  bin/dispatch.py record o/r#n <session id>
      -> writes session, session_url and run (+ ⚠️ Run: notes) into the status
         comment, keeping whatever phase the worker has reached

The model comes from the issue's labels as they are at `prepare`, else the
project's `models:` default for the session's stage (bin/run_config.py). The
stage is `design` for a new worker, `build` with `--directive BUILD` (the
hand-off to a build session, see bin/plan.py), and for a redispatch whatever
stage the status JSON's `run` already records.
`append_system_prompt` is the short loader from `bin/playbook.py --stub`, or the
full playbook when bot.config.yml sets `playbook_delivery: inline` (or with
--inline).

Options for prepare:
  --orchestrator ID   this orchestrator's session id (required)
  --directive D       DESIGN (default), RESUME, or BUILD (hand-off: a fresh
                      build-stage session for an approved issue; implies
                      --redispatch and --stage build)
  --stage S           design or build: override the stage picked above
  --notes FILE        orchestrator notes to append to the brief ("-" = stdin)
  --redispatch        allow an issue whose status already names a session
                      (blocked worker, claude:ping on a dead one); the phase is
                      left as it is
  --inline            put the full playbook in append_system_prompt
  --dry-run           write nothing to GitHub; print the arguments
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import playbook  # noqa: E402
import run_config  # noqa: E402
import status as st  # noqa: E402

CACHE = os.path.expanduser("~/.cache/claude-bot/dispatch")
DASHBOARD_TITLE = "claude-bot queue"


def load_config():
    import yaml
    config = yaml.safe_load(open(os.path.join(ROOT, "bot.config.yml")))
    defaults = config.get("defaults") or {}
    projects = {p["repo"]: {**defaults, **p} for p in config.get("projects") or []}
    return config, projects


def settings_yaml(settings):
    """The merged project settings as YAML, notes as a block."""
    lines = []
    for key, value in sorted(settings.items(), key=lambda item: item[0] != "repo"):
        if key == "notes":
            continue
        lines.append(f"{key}: {json.dumps(value) if not isinstance(value, str) or ':' in value else value}")
    if settings.get("notes"):
        lines.append("notes: |")
        lines += ["  " + line if line else "" for line in str(settings["notes"]).rstrip("\n").split("\n")]
    return "\n".join(lines)


def slug(title):
    words = re.sub(r"[^a-z0-9]+", " ", title.lower()).split()
    out = ""
    for word in words:
        if len(out) + len(word) + 1 > 30:
            break
        out = f"{out}-{word}" if out else word
    return out or "issue"


def dashboard_url():
    repo = playbook.this_repo()
    for issue in st.gh(f"repos/{repo}/issues?state=open&per_page=100"):
        if issue["title"] == DASHBOARD_TITLE and "pull_request" not in issue:
            return issue["html_url"]
    return f"https://github.com/{repo}/issues"


def cache_path(repo, number):
    return os.path.join(CACHE, f"{repo.replace('/', '-')}-{number}.json")


def brief(repo, number, title, directive, orchestrator, dashboard, comment_id, owners, branch, settings, notes):
    text = f"""You are the claude-bot worker for {repo}#{number}: "{title}".
Directive: {directive}
Orchestrator session: {orchestrator}
Dashboard: {dashboard}
Status comment id: {comment_id}
Owners: {", ".join(owners)}
Branch: {branch}
Project settings:
```yaml
{settings_yaml(settings)}
```
Follow the claude-bot worker playbook in your system prompt (load it first if your system prompt says how)."""
    if notes:
        text += "\n\nOrchestrator notes:\n" + notes.strip()
    return text


def pick_stage(directive, status, stage=None):
    """The stage a new session is for: explicit, BUILD -> build, else the
    stage already recorded (a redispatch keeps it), else design."""
    if stage:
        return stage
    if directive == "BUILD":
        return "build"
    return ((status or {}).get("run") or {}).get("stage") or "design"


def prepare(ref, orchestrator, directive="DESIGN", notes=None, redispatch=False, inline=None, dry_run=False,
            stage=None):
    repo, number = st.parse_ref(ref)
    config, projects = load_config()
    if repo not in projects:
        raise RuntimeError(f"{repo} isn't a project in bot.config.yml")
    settings = projects[repo]
    owners = config.get("owners") or []
    issue = st.gh(f"repos/{repo}/issues/{number}")
    labels = [label["name"] for label in issue["labels"]]
    comments = st.issue_comments(repo, number)
    comment_id, status, body = st.find(repo, number, comments)
    if status and status.get("session") and not redispatch:
        raise RuntimeError(f"{repo}#{number} already has worker {status['session']} (phase {status.get('phase')}); "
                           "use --redispatch to start a fresh one")
    owner_ids = [c["id"] for c in comments
                 if c["user"]["login"] in owners and not (c.get("body") or "").startswith(st.BOT_MARKER)]
    handled = max(owner_ids, default=0)
    stage = pick_stage(directive, status, stage)
    run = run_config.resolve(labels, stage, run_config.stage_models(config, repo))
    branch = (status or {}).get("branch") or f"claude/issue-{number}-{slug(issue['title'])}"

    if not dry_run:
        if comment_id is None:
            new = {"phase": "dispatching", "session": None, "session_url": None, "run": run["run"],
                   "playbook": playbook.version(), "branch": branch, "pr": None, "design": None, "lane": None,
                   "preview": None, "waiting_since": None, "handled_through": handled, "updated": st.now_stamp()}
            created = st.create(repo, number, st.render(new), new)
            comment_id = created["id"]
            add, remove = st.phase_label_changes(labels, "dispatching")
            st.set_labels(repo, number, add, remove)
        elif redispatch:
            # A fresh worker for an issue already under way: leave the phase alone.
            st.patch(repo, number, {"run": run["run"], "branch": branch}, found=(comment_id, status, body))
        else:
            changes = {"run": run["run"], "branch": branch, "handled_through": max(handled, status.get("handled_through") or 0)}
            st.patch(repo, number, changes, phase="dispatching", found=(comment_id, status, body), labels=labels)

    use_inline = inline if inline is not None else settings.get("playbook_delivery",
                                                                  config.get("playbook_delivery")) == "inline"
    if use_inline:
        system = playbook.body() + f"\nPlaybook version: {playbook.version()} (record it as `playbook` in your status JSON).\n"
    else:
        system = playbook.stub()
    args = {
        "source_url": f"https://github.com/{repo}",
        "title": f"claude-bot: {repo}#{number} {issue['title']}"[:80],
        "tags": ["claude-bot", f"claude-bot:{repo}#{number}"],
        "outcome_branch": branch,
        "prompt": brief(repo, number, issue["title"], directive, orchestrator, dashboard_url(), comment_id or "<new>",
                        owners, branch, settings, notes),
        "append_system_prompt": system,
    }
    if run["model_id"]:
        args["model"] = run["model_id"]
    meta = {"run": run["run"], "notes": run["notes"], "comment_id": comment_id}
    if not dry_run:
        os.makedirs(CACHE, exist_ok=True)
        json.dump({"args": args, "meta": meta}, open(cache_path(repo, number), "w"), indent=1)
    return args, meta


def record(ref, session_id):
    repo, number = st.parse_ref(ref)
    try:
        meta = json.load(open(cache_path(repo, number)))["meta"]
    except (OSError, ValueError, KeyError):
        # No prepare in this container (recycled?): re-resolve the labels.
        labels = [label["name"] for label in st.gh(f"repos/{repo}/issues/{number}/labels?per_page=100")]
        _, status, _ = st.find(repo, number)
        stage = pick_stage(None, status)
        resolved = run_config.resolve(labels, stage, run_config.load_stage_models(repo))
        meta = {"run": resolved["run"], "notes": resolved["notes"]}
    changes = {"session": session_id, "session_url": f"https://claude.ai/code/{session_id}", "run": meta["run"]}
    st.patch(repo, number, changes, notes=[f"⚠️ Run: {n}" for n in meta["notes"]])


def main(argv):
    if len(argv) < 2 or argv[0] not in ("prepare", "record"):
        print(__doc__)
        return 2
    command, ref, rest = argv[0], argv[1], argv[2:]
    if command == "record":
        if not rest:
            raise SystemExit("record needs the session id")
        record(ref, rest[0])
        print(f"recorded {rest[0]} on {ref}")
        return 0
    opts = {"orchestrator": None, "directive": "DESIGN", "notes": None, "redispatch": False, "inline": None,
            "dry_run": False, "stage": None}
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag in ("--redispatch", "--inline", "--dry-run"):
            opts[flag[2:].replace("-", "_")] = True
            i += 1
            continue
        value = rest[i + 1]
        if flag == "--orchestrator":
            opts["orchestrator"] = value
        elif flag == "--directive":
            opts["directive"] = value.upper()
        elif flag == "--stage":
            if value not in run_config.STAGES:
                raise SystemExit("--stage must be design or build")
            opts["stage"] = value
        elif flag == "--notes":
            opts["notes"] = sys.stdin.read() if value == "-" else open(value).read()
        else:
            raise SystemExit(f"unknown option {flag}")
        i += 2
    if not opts["orchestrator"]:
        raise SystemExit("--orchestrator <session id> is required")
    if opts["directive"] not in ("DESIGN", "RESUME", "BUILD"):
        raise SystemExit("--directive must be DESIGN, RESUME or BUILD")
    if opts["directive"] == "BUILD":
        opts["redispatch"] = True
    args, meta = prepare(ref, **opts)
    sys.stderr.write(f"{ref}: comment {meta['comment_id']}, stage {meta['run'].get('stage')}, "
                     f"model {args.get('model') or 'default'}, "
                     f"prompt {len(args['prompt'])} B, system {len(args['append_system_prompt'])} B"
                     + "".join(f"\n  note: {n}" for n in meta["notes"]) + "\n")
    json.dump(args, sys.stdout, indent=1, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
