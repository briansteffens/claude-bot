---
name: bot-setup
description: One-time (and re-runnable) setup for claude-bot — make this session the orchestrator, give it access to every configured project, create the claude:* labels, check merge/preview prerequisites, schedule the hourly heartbeat, and run the first tick. Use when first installing the bot, after adding a project to bot.config.yml, or to move the orchestrator to a new session.
---

# /bot-setup

Re-runnable: every step checks before it creates.

1. **Config.** `git pull --ff-only`, read `bot.config.yml`. If `projects` is empty,
   ask the owner which repos to add, write them in, commit and push.

2. **Mode.** Resolve `mode` as `/tick` does. Tell the owner which mode you'll run in.
   - `sessions` needs `create_session`, `send_message`, `create_trigger` (cloud).
   - `inline` needs `gh` authenticated (`gh auth status`) or the GitHub MCP tools.

3. **Repo access (cloud).** For each project, `add_repo` with `access: "push"` so
   this session can label and comment. Relay any access error verbatim with the
   remedy it names, and skip that project until fixed.

4. **Labels.** In each project, create any missing labels:

   | Label | Color | Description |
   | --- | --- | --- |
   | `claude` | `d97757` | Queue for claude-bot |
   | `claude:next` | `d97757` | claude-bot: do this one next |
   | `claude:paused` | `bfbfbf` | claude-bot: leave alone |
   | `claude:ping` | `d93f0b` | claude-bot: worker seems stuck, nudge it |
   | `claude:designing` | `c5def5` | claude-bot: writing design |
   | `claude:needs-input` | `fbca04` | claude-bot: waiting on you |
   | `claude:approved` | `0e8a16` | claude-bot: design approved |
   | `claude:building` | `1d76db` | claude-bot: implementing |
   | `claude:in-review` | `5319e7` | claude-bot: PR open |
   | `claude:blocked` | `b60205` | claude-bot: stuck, see status |
   | `claude:done` | `0e8a16` | claude-bot: merged |

   The GitHub MCP server has no label-create tool. Use the REST API through `gh`:
   `gh api repos/<o>/<r>/labels -f name=<name> -f color=<c> -f description=<d>`
   (ignore "already_exists"). Avoid `gh label create`: it goes through GraphQL,
   which cloud sessions block. If that fails too, list the missing labels for the
   owner and note that GitHub also creates a label the first time it's added to
   an issue.

5. **Prerequisites per project**, reported as a checklist (don't change repo
   settings yourself):
   - `merge: auto-merge` → repo has "Allow auto-merge" on and the default branch
     has a protection rule (required review and/or checks); otherwise auto-merge
     can't be enabled.
   - `preview: pages` → the repo's Pages workflow also publishes the `previews`
     branch under `/previews/`, and a push to `previews` alone redeploys (README
     "Playable previews"). If not, suggest the owner queue an issue for it.
   - The default branch's CI is green (a red base blocks every PR).

6. **Heartbeat (sessions mode).** `list_triggers` and look for `claude-bot heartbeat`.
   - Missing → `create_trigger`: name `claude-bot heartbeat`, cron `0 * * * *`,
     prompt `Run /tick (claude-bot heartbeat).`, fires into this session (default
     target), initiation `human_request`.
   - Exists but bound to a different session → this is a move: confirm with the
     owner, then delete it and create a new one bound here.
   - In inline/local mode skip this; tell the owner to run `/loop 10m /tick`
     (or use `./run-local.sh`).

7. **Dashboard.** If `dashboard_issue` is on, create the `claude-bot queue` issue in
   this repo if it doesn't exist.

8. **First tick.** Run `/tick` and report the result, plus how to queue work:
   add the `claude` label to an issue.
