# claude-bot

An issue-driven development bot that runs **inside Claude Code**, not as a script
calling the API. You queue issues by labeling them `claude`; the bot works
through each project's queue one issue at a time. Straightforward issues go
straight to a PR with a playable preview; bigger ones start as a draft PR holding
a design and questions, which you discuss right there before the bot builds on the
same PR. You merge, and the next issue starts. Every step
happens in a Claude Code session you can open, read (including its reasoning) and
type into, while GitHub issues stay the main interface.

## How it works

```
                         ┌──────────────────────────────────────┐
  hourly heartbeat ───▶  │ Orchestrator session (this repo)     │ ◀── you: "pause #15",
  + 10-min polls while   │ /tick: read bot.config.yml, scan     │     "what's queued?"
    work is in flight    │ labels + status comments, hand out   │
                         │ build slots, dispatch/nudge workers  │
                         └──────┬───────────────┬───────────────┘
                     create_session │      send_message │ BUILD / ACTIVITY / MERGED
                                ▼                   ▼
          ┌───────────────────────────┐   ┌───────────────────────────┐
          │ Worker: my-game#12        │   │ Worker: my-game#15        │  one session per
          │ in-review (holds a slot)  │   │ needs-input: draft design │  issue, open them
          │ subscribed to its PR      │   │ PR, subscribed to it      │  in the Claude app
          └─────────────┬─────────────┘   └─────────────┬─────────────┘
                        ▼                               ▼
        GitHub: status comment + claude:<phase> label on each issue; draft design
        PRs and PRs with "▶️ Try it" links  ──▶  you comment / approve / merge on the PR
```

- **Two build slots per project, filled only with work that won't collide.** The
  queue head always gets the first slot. Before handing out another, the
  orchestrator has a subagent compare the candidates against what's already
  building: their footprints (files and systems each change touches, recorded by
  the workers) and the in-progress diffs. A music change can build next to a
  rework of how a bear moves; a UI bug fix next to a new biome; two changes to
  the same HUD code wait their turn. When one PR merges, the other worker merges
  the new base, re-checks and refreshes its preview. (`build_slots: 1` makes it
  strictly sequential; `build_scope: global` shares the slots across repos.)
- **Design only when it's worth it.** With `design_stage: auto` the worker skips
  the design round for straightforward issues (posting its plan and assumptions on
  the issue so you can object) and goes straight to building. Issues with real
  questions get a **draft PR** holding the design; the implementation later lands
  on that same PR.
- **Conversations happen on the PR, instantly.** Each worker subscribes to its PR,
  so your comments and reviews wake it within seconds; no polling involved.
- **Designs run ahead.** While one issue is being built or reviewed, the next
  `design_ahead` issues can have design PRs open, so your answers aren't on the
  critical path. Design PRs only touch their own design file, so they don't conflict.
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
| designing | `claude:designing` | Worker triages: straightforward → posts plan and skips to approved; otherwise opens a draft design PR | — |
| needs-input | `claude:needs-input` | Draft PR with design + numbered questions 🔔 | Answer on the PR. Once nothing is unclear the bot moves on by itself; no "approve" needed |
| approved | `claude:approved` | Waits for a build slot (a second slot only if it won't collide with what's building) | — |
| building | `claude:building` | Implements on the PR branch (merged up to the latest base), tests, plays it in headless Chromium | — |
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
**subscribe to their PR's activity**, so comments, reviews and CI results wake them
immediately. The orchestrator's polling only matters for queueing (labels) and for
the occasional comment left on the issue instead of the PR.

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

- Anything on a PR reaches the worker immediately. Newly labelled issues and
  comments on the issue itself wait for the next poll: ~10 minutes while work is
  in flight, otherwise the hourly heartbeat. Say "check now" in the orchestrator
  session to run `/tick` immediately.
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
