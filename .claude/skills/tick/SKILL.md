---
name: tick
description: Run one orchestration pass of claude-bot — scan every configured project's queued issues, advance their state, dispatch or nudge worker sessions, finalize merged work, refresh the dashboard, and schedule the next pass. Use when the heartbeat fires, when a worker pings "tick", or when the owner says "check now".
---

# /tick — one orchestration pass

A tick must be **idempotent**: running it twice in a row changes nothing the second
time. All state lives on GitHub (see CLAUDE.md), so re-derive everything from there.

## 0. Prepare

1. `git pull --ff-only` in this repo, then read `bot.config.yml`. Merge each
   project's settings over `defaults`.
2. Load GitHub tools (ToolSearch `+github issue`, `+github pull`) or confirm
   `gh auth status` works.
3. Resolve the mode. `auto` → `sessions` if `mcp__claude-code-remote__create_session`
   is available, else `inline`.
4. Note this session's id (`get_session` with no id) — workers need it to ping you.

## 1. Gather

For each project, list **open** issues labeled `claude`, plus issues closed in the
last 7 days that carry a non-terminal `claude:*` phase label. For each issue:

- Find the status comment: the comment whose body starts with
  `<!-- claude-bot:status`. Parse its JSON (schema in the work-issue skill). No
  status comment → phase `queued`.
- Collect owner comments (author in `owners`, body without `<!-- claude-bot`) with
  id greater than `handled_through`.
- If the state has a `pr`, read the PR's state (open / merged / closed).
- In `sessions` mode, if the state has a `session`, `get_session` it and note
  `status_bucket` (working / blocked / review_ready / completed / failed).

Queue order within a project: `claude:next` first, then by issue number ascending.

## 2. Reconcile each issue

Apply the first matching rule:

| Condition | Action |
| --- | --- |
| Label `claude:paused` | Skip entirely. |
| Phase `done` / `cancelled` | Nothing. |
| PR merged, phase not `done` | Finalize as the playbook's "Merged" step describes (status → `done`, label, summary comment, close the issue if still open, notify). Tell the worker `MERGED` so it unsubscribes. |
| Issue closed without a merged PR | Status → `cancelled`, remove `claude:*` phase labels, send worker `CANCEL`. Leave any PR for the owner. |
| Phase `dispatching` with no session id | A previous tick died mid-dispatch: `list_sessions` with tag `claude-bot:<repo>#<n>`; adopt a match, otherwise dispatch again. |
| Worker `status_bucket` = `failed`, or session missing | Status → `blocked` with the reason, notify the owner. Owner can reply "retry" to get a fresh worker. |
| Phase `blocked` and an unhandled owner comment says retry/continue | Dispatch a fresh worker with directive `RESUME` (reuse the session if it still exists). |
| Unhandled owner comments, or label `claude:approved` while phase is `designing`/`needs-input` | Nudge the worker with `ACTIVITY` — unless its `status_bucket` is `working` (it will see them when it re-reads). |
| Otherwise | Nothing. |

## 3. Build lock and dispatch

Per project (or across all projects when `build_lock: global`):

1. **Lock holder** = the issue in phase `building` or `in-review`. At most one.
2. If there is no holder, take the first issue in queue order whose phase is
   `approved` and send its worker `BUILD` (in inline mode: do the build now). The
   worker sets `building` itself; record nothing here.
3. **Design ahead.** Count issues in `dispatching`/`designing`/`needs-input`/`approved`.
   While that count is below `design_ahead`, take the next `queued` issue in queue
   order and dispatch it with directive `DESIGN`.

Never start two builds in one project. Never dispatch the same issue twice — check
the status comment again right before dispatching.

### Dispatching a worker (sessions mode)

1. Post the status comment (template in the work-issue skill) with
   `phase: "dispatching"`, `handled_through` = id of the latest owner comment that
   is already in the issue (0 if none), and add label `claude:designing`.
2. Read `.claude/skills/work-issue/SKILL.md`, drop its front-matter.
3. `create_session`:
   - `source_url`: `https://github.com/<owner>/<repo>`
   - `title`: `claude-bot: <owner>/<repo>#<n> <issue title>` (truncate to ~80 chars)
   - `tags`: `["claude-bot", "claude-bot:<owner>/<repo>#<n>"]`
   - `outcome_branch`: `claude/issue-<n>-<short-slug>`
   - `append_system_prompt`: the playbook text from step 2
   - `prompt`: the worker brief below
4. Update the status comment with `session` and `session_url`
   (`https://claude.ai/code/<session id>`), keeping `phase: "dispatching"`; the
   worker moves it to `designing`. Re-read it first (the worker may already have
   filled these in) and write it with `update_issue_comment`, not `gh api`: in
   cloud sessions the proxy wraps session URLs in `gh api` bodies in backticks,
   which breaks the status JSON.

Worker brief:

```
You are the claude-bot worker for <owner>/<repo>#<n>: "<title>".
Directive: DESIGN
Orchestrator session: <this session id>
Status comment id: <comment id>
Owners: <logins>
Branch: claude/issue-<n>-<short-slug>
Project settings:
<the merged settings for this project as YAML, including notes>
Follow the claude-bot worker playbook in your system prompt.
```

### Messages to workers

Send with `send_message` (priority `later`) to the issue's session. One line each;
the worker re-reads GitHub for details:

- `BUILD` — you hold the build lock; start implementing.
- `ACTIVITY` — new owner input on the issue; re-read and respond.
- `MERGED` / `CANCEL` — wrap up: unsubscribe from PR activity and stop.
- `RESUME` — you were blocked; re-read the issue and continue.

In inline mode, perform the same step yourself by following the work-issue skill.

## 4. Dashboard

If `dashboard_issue` is true, keep one open issue in this repo titled
`claude-bot queue` (create it if missing; find it by title). Rewrite its body only
when the content changed:

```
<!-- claude-bot -->
_Updated <UTC time>_

### <owner>/<repo>
| # | Issue | Phase | PR | Session |
| --- | --- | --- | --- | --- |
| 1 | #12 Add jump | 🔨 building | — | [open](https://claude.ai/code/...) |
| 2 | #15 High scores | ❓ needs input | — | [open](...) |
| 3 | #18 Sound | ⏳ queued | — | — |
```

## 5. Schedule the next tick

The recurring `claude-bot heartbeat` scheduled task (created by `/bot-setup`) runs
hourly regardless. Poll faster while things are moving:

- **Active** = any issue in `dispatching`/`designing`/`needs-input`/`building`/`in-review`,
  or an owner comment in the last 2 hours.
- If active: make sure exactly one enabled one-shot scheduled task named
  `claude-bot next tick` exists firing into this session `active_poll_minutes`
  from now. `list_triggers` (recurring: false, enabled: true) to find it; move it
  with `update_trigger` `run_once_at`, or create it with `send_later`
  (`name: "claude-bot next tick"`, message `Run /tick (claude-bot poll).`).
- If idle: leave the heartbeat to it; don't create a one-shot.
- In inline/local mode under `/loop`, skip this step — the loop paces itself.

## 6. Report

End with a compact summary, e.g.:

```
briansteffens/my-game: #12 building · #15 needs-input (2 questions) · #18 queued
briansteffens/site: idle
```

Call out anything that needs the owner. Don't send push notifications from the
tick itself except for finalize/blocked events above — workers notify for their own
milestones.
