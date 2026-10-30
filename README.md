# claude-bot

An issue-driven development bot that runs **inside Claude Code**, not as a script
calling the API. You queue issues by labeling them `claude`; the bot works
through each project's queue one issue at a time: design proposal and questions,
your answers, a PR with a playable preview, merge, then the next issue. Every step
happens in a Claude Code session you can open, read (including its reasoning) and
type into, while GitHub issues stay the main interface.

## How it works

```
                         ┌──────────────────────────────────────┐
  hourly heartbeat ───▶  │ Orchestrator session (this repo)     │ ◀── you: "pause #15",
  + 10-min polls while   │ /tick: read bot.config.yml, scan     │     "what's queued?"
    work is in flight    │ labels + status comments, hold the   │
                         │ build lock, dispatch/nudge workers   │
                         └──────┬───────────────┬───────────────┘
                     create_session │      send_message │ BUILD / ACTIVITY / MERGED
                                ▼                   ▼
          ┌───────────────────────────┐   ┌───────────────────────────┐
          │ Worker: my-game#12        │   │ Worker: my-game#15        │  one session per
          │ in-review (holds lock)    │   │ needs-input (design only) │  issue, open them
          │ subscribed to PR events   │   │                           │  in the Claude app
          └─────────────┬─────────────┘   └─────────────┬─────────────┘
                        ▼                               ▼
        GitHub: status comment + claude:<phase> label on each issue, design
        comments, PRs with "▶️ Try it" preview links  ──▶  you reply / approve / merge
```

- **One build at a time per project.** Only one issue per repo can be `building`
  or `in-review`. The next build starts from the freshly merged default branch, so
  PRs don't conflict. (Set `build_lock: global` for one at a time across all repos.)
- **Designs run ahead.** While one issue is being built or reviewed, the next
  `design_ahead` issues get design proposals and questions, so your answers aren't
  on the critical path.
- **One session per issue.** Each worker session is titled
  `claude-bot: owner/repo#N …` and linked from the issue's status comment. Open it to
  watch its thinking, or type into it to steer ("use a tilemap instead").
- **State lives on GitHub** (a status comment with hidden JSON plus a
  `claude:<phase>` label), so containers can be recycled, context can be compacted,
  and the orchestrator can be replaced without losing anything.
- **Notifications** go out as push notifications from the session at the moments
  you need to act — design questions posted, PR ready to play, blocked, merged —
  plus the usual GitHub notifications and a `claude-bot queue` dashboard issue in
  this repo.

### Lifecycle of an issue

| Phase | Label | What happens | Your move |
| --- | --- | --- | --- |
| queued | `claude` | Waiting its turn | — |
| designing | `claude:designing` | Worker reads the code, writes a proposal | — |
| needs-input | `claude:needs-input` | Design + numbered questions posted 🔔 | Answer, request changes, or say **approve** (or add `claude:approved`) |
| approved | `claude:approved` | Waits for the project's build lock | — |
| building | `claude:building` | Implements on a fresh branch, tests, plays it in headless Chromium | — |
| in-review | `claude:in-review` | PR open with a **▶️ Try it** link 🔔; fixes CI and review feedback | Play it, review, merge |
| done | `claude:done` | Wrap-up comment, issue closed 🔔, next issue starts | — |
| blocked | `claude:blocked` | Says what it needs 🔔 | Reply, e.g. "retry" |

You can reply in plain language — the bot interprets intent rather than parsing
commands. Labels `claude:next` (jump the queue) and `claude:paused` (leave alone)
work at any time.

## Why this design (and not a script)

| Option | See/steer the conversation? | Verdict |
| --- | --- | --- |
| Python script on the Claude API / Agent SDK | No — only logs | Ruled out by your requirement |
| `claude-code-action` in GitHub Actions | Logs only, one-shot per event | Not interactive |
| **Claude Code cloud sessions + scheduled tasks** (this repo) | Yes — every session is in claude.ai/code and the app, live, and you can type into it | **Recommended** |
| Local `claude` in tmux with `/loop` | Yes — attach to the terminal | Good alternative if you want it on your machine |

The recommended setup uses Claude Code on the web: a long-lived orchestrator session
woken by a **scheduled task** (hourly heartbeat, plus self-scheduled 10-minute polls
while something is in flight), spawning a **worker session** per issue. Workers
**subscribe to their PR's activity**, so reviews and CI results wake them
immediately; issue comments are picked up by the orchestrator's next poll.

The bot is just Claude Code configuration — `CLAUDE.md`, three skills and a YAML
file — so the same repo also runs locally.

## Setup — cloud (recommended)

1. Edit `bot.config.yml`: add your repos under `projects`, check `owners`, and pick
   `merge` and `preview` modes. Commit and push.
2. In [claude.ai/code](https://claude.ai/code), start a new session on this repo.
   Use an environment whose network access allows installing your projects'
   dependencies, and a permission mode that won't stop for prompts (workers inherit
   it and can't be more permissive than the orchestrator).
3. In that session run `/bot-setup`. It gives the session access to each project,
   creates the `claude:*` labels, checks merge/preview prerequisites, creates the
   hourly `claude-bot heartbeat` scheduled task bound to that session, and runs the
   first `/tick`.
4. Add the `claude` label to an issue.

Keep that session — it's the orchestrator. Rename it something like
"claude-bot orchestrator". To move it, run `/bot-setup` in a new session; it
re-binds the heartbeat after asking.

## Setup — local

```bash
gh auth login
```

```bash
./run-local.sh --permission-mode acceptEdits
```

```bash
tmux attach -t claude-bot
```

Locally there's no per-issue session spawning, so the orchestrator works inline
(`mode: auto` picks this): one session does everything, still one issue at a time
per project. Run `/bot-setup` once in it to create labels. Set
`CLAUDE_BOT_INTERVAL` to change the poll interval (default `10m`). If the bot
posts under your own GitHub account you won't get GitHub notifications for its
comments; push notifications still reach you when Remote Control is on, or
authenticate `gh` as a separate machine user.

## Merging

- `merge: manual` (default) — the bot says the PR is ready to play and merge; you
  press merge. The next tick sees the merge and moves on.
- `merge: auto-merge` — the bot enables GitHub auto-merge on its PR, and GitHub merges
  when your branch protection is satisfied (e.g. your approving review + green CI).
  Needs "Allow auto-merge" in the repo settings and a protection rule on the default
  branch.

The bot never merges because of something it read on GitHub; merging is either your
click or GitHub's auto-merge enforcing rules you set.

## Playable previews

- `artifact` — the worker publishes a playable build as a claude.ai Artifact (private
  to you) and links it from the PR. Best for browser games and web UIs.
- `pages` — builds go to `gh-pages` under `previews/pr-<n>/`.
- `instructions` — copy-paste run commands plus captured output.

In every mode the worker loads the build in headless Chromium with Playwright and
exercises the new behaviour before opening the PR.

## Safety

- Only `owners` can direct the bot. Other people's issue bodies and comments are
  treated as data — useful context, never instructions — so a public issue can't
  make the bot touch secrets, workflows, other repos, or merge.
- The bot marks every comment it writes with `<!-- claude-bot -->`, so it can tell its
  own comments from yours even when both are posted under your account.

## Limits worth knowing

- Scheduled tasks normally run at most hourly; the bot adds one-shot 10-minute polls
  while work is in flight, so an issue reply is typically picked up within ~10
  minutes. Say "check now" in the orchestrator session to run `/tick` immediately.
- PR reviews and CI reach the worker immediately through PR subscriptions.
- A worker that hits a permission prompt waits for you; you'll see it in the session.
- Finished worker sessions stay in your session list; archive them when you like.

## Files

| Path | Purpose |
| --- | --- |
| `bot.config.yml` | Projects, owners, policies |
| `CLAUDE.md` | Orchestrator rules, label reference |
| `.claude/skills/tick/SKILL.md` | One orchestration pass |
| `.claude/skills/work-issue/SKILL.md` | Worker playbook: design → build → PR → merge |
| `.claude/skills/bot-setup/SKILL.md` | Setup and re-binding |
| `run-local.sh` | Run locally in tmux |
