# Design for #15: fable for planning, opus for implementation

## Feasibility

Yes. A worker session's model is fixed when the orchestrator calls
`create_session`; nothing can switch it later. So "different model per stage"
means **a different session per stage**: a design worker, then a fresh build
worker when the issue gets `BUILD`. That works because all state already lives
on GitHub (status comment, design PR, branch), which is exactly what a
restarted worker reads today.

## Approach (recommended: stage-split sessions)

- `bot.config.yml` gets optional per-stage defaults (global `defaults:` and per
  project):
  ```yaml
  models:
    design: fable   # triage + design round
    build: opus     # implementation, review, sync
  ```
  Unset = today's behaviour (one session, `create_session` default model).
- Label precedence: a `model:*` label on the issue still wins and pins **both**
  stages (one session, as now). No label → stage defaults.
- `bin/run_config.py` grows a `stage` argument (`design`/`build`) that applies
  the config default when no label is set; the alias → ID table stays the one
  place for IDs.
- Dispatch: `bin/dispatch.py prepare` resolves the `design` model as now.
  The status JSON's `run` records `{"model": "fable", "stage": "design", ...}`.
- Hand-off at BUILD: when `bin/plan.py` picks an issue for a build slot and the
  build model differs from the session's `run.model`, it emits a `handoff`
  action instead of a plain `build`. The tick then:
  1. sends the old worker `CANCEL`-like `HANDOFF` (new directive: "unsubscribe
     from PR activity, stop; don't touch the status phase"),
  2. `bin/dispatch.py prepare --redispatch --stage build --directive BUILD`,
     `create_session`, `record` (session, `run` for the build stage),
  The new worker starts at Build (re-reads the issue, design PR and comments,
  re-subscribes to the PR). Same model for both stages → plain `BUILD`, no new
  session.
- Playbook: a worker briefed with directive `BUILD` (not just messaged it) goes
  straight to Build after re-reading everything. One short paragraph; older
  workers never get `HANDOFF`, because plan.py only emits it for issues whose
  `run` has a `stage` field (i.e. dispatched after this change).
## Decisions (owner, rev 1)

1. On for all: `defaults: models: {design: fable, build: opus}`.
2. Quick lane: no hand-off; the triage session builds it on the design model.
3. Fast-path issues hand off too (same rule as design-PR issues).
4. The design session is left open at hand-off (not archived).

## What the owner sees

- Dashboard/status comment: `Run: fable (design)` → `Run: opus (build)`; the
  "Live session" link moves to the build session at BUILD.
- One extra session per non-quick issue in the claude.ai session list.

## Alternatives considered

- **One session, subagents for the other stage** (e.g. an opus worker that has
  a `fable` subagent draft the design). No hand-off, but the owner can't watch
  the subagent's reasoning, the main session's model still does all the
  judgement, and it's easy to drift. Not really "fable plans".
- **Owner switches the model by hand** in the session UI. Works today, no code,
  but it's manual every time.

## Out of scope

- Per-stage effort (the session API still has no effort setting).
- Changing models of sessions already running.

## Plan

- `bin/run_config.py`: `stage`, config defaults, self-tests for precedence.
- `bin/dispatch.py`: `--stage`, `--directive BUILD`, record `run.stage`.
- `bin/plan.py`: `handoff` action; `--apply` side.
- `.claude/skills/tick/SKILL.md`: hand-off steps; `work-issue`: `HANDOFF`
  directive + "briefed with BUILD" paragraph (bumps playbook version).
- `bot.config.yml` + README + CLAUDE.md: document `models:`.
- Verify: `run_config.py --test`, `plan.py`/`dispatch.py --dry-run` against the
  live snapshot, `py_compile`.
