# Design #8: a quick lane for small data-only changes

## Goal
A data-only issue ("add a WRX at the Ferry Building") goes from the `claude` label to a
PR with a playable preview in about 15–20 minutes, not 50+.

## Where the time goes today (bay-area-pirates#152, estimated S / 12 min, ~50 min with no PR)
1. Dispatch: a new session, cloning, reading the repo (unavoidable, a few minutes).
2. Triage, then a fast-path plan comment, then `approved`.
3. Waiting for the orchestrator's next tick to send `BUILD` (and for a free slot).
4. `npm ci` plus the full build, started only after `BUILD`.
5. Publishing the preview and waiting for the Pages deploy before the play-check can start.
6. **The play-check, which took most of the time:** the playbook asks for "exercise the new
   behaviour", and the worker spent it on deep driving-physics tests (gears, clutch, slopes, hill
   starts) of a vehicle whose physics it didn't change.
7. No `progress` was written, so nothing showed that it was going long.

## Approach

### 1. A "quick" lane, picked in Triage
- The worker puts an issue in the quick lane when **all** of these hold:
  - it adds or changes **entries in existing data**: a spawn/registry entry, a config value,
    copy text, a colour, a position;
  - it follows a pattern already in the repo (another entry of the same kind exists to copy);
  - no new code paths: no new functions, systems, shaders or file formats;
  - about 3 files or fewer.
- The owner can force it with a **`lane:quick`** label. It works like `model:*`: not a
  `claude:` label, so phase-label tidying, `watch-new.py` and old workers all leave it alone.
- The worker can also be **kept out** of the quick lane: `design_stage: always`, or the
  project's `quick_lane: false`.
- Recorded in the status JSON as `"lane": "quick"`, with an S estimate.
  A missing `lane` means `normal`, so workers on the old playbook carry on as they are.

### 2. Start building right away
- Quick-lane workers **skip the plan comment**. The PR body's "What changed" and Notes
  sections carry the plan and its assumptions instead.
- The worker goes to `approved` with `lane: "quick"` and pings `tick` at once. The tick
  that ping triggers gives quick issues their own **quick slot**, separate from
  `build_slots` (1 per project by default, `quick_slots` in the config), and sends `BUILD`
  within a minute or two. The orchestrator stays the only one doing slot accounting, so two
  workers can't race for the same slot.
- Parallelism: a quick build next to the holders still gets the usual check, but a pure
  additive data entry is "safe" unless a holder edits the same entries. Two quick issues
  never build at once in a project (that's what the slot limit is for), so a data file isn't
  edited by two quick builds at the same time.
- Projects with `preview: deploy` (bar-manager: every push redeploys the one live site)
  get no quick slot: quick issues there wait for a normal slot, but still keep every other
  speed-up.

### 3. Overlap the slow steps (all lanes)
- Start the dependency install (`npm ci` etc.) **in the background during Triage**, so it's
  done by `BUILD`.
- With `preview: pages` / `deploy`: push the preview, then **play-check the local build while
  Pages deploys**. Once the deploy is live, just confirm `preview-version.txt` shows the SHA and
  the page loads without console errors. No second full play-check at the URL.

### 4. A play-check scoped to the diff, with a time box
- Quick lane: check **only what the change touches**, with a **10-minute** box. For a spawn
  entry: it loads, it's at the right place and heading, it renders, one basic interaction
  (e.g. you can board or enter it), no new console errors. Don't test systems the diff
  didn't change.
- Normal lane: the same "scope it to the diff" rule, with no hard box.
- When the box runs out: stop, open the PR anyway, and write what was and wasn't verified
  under Notes.

### 5. A hard cap, and stricter progress
- `progress` is **required** in every lane: at the start of each stage, after each step,
  and **before** any step that takes more than ~5 minutes (install, build, deploy wait,
  play-check), so a long-running step shows up as such.
- Quick-lane cap: **2× the estimate** (`quick_cap_minutes`, 25 by default) from `BUILD`.
  At the cap, open the PR with what's done and the open findings under Notes, and hand it
  to the owner. They can say "keep going".
- If the work turns out to need logic, the worker **leaves the quick lane**: it sets
  `lane: "normal"`, re-estimates, says why in `progress.step`, and carries on as a normal
  fast-path build. It doesn't stop to ask unless a real decision comes up.

## What the owner sees
- A new issue labelled `claude` (optionally `lane:quick`) gets a PR with a ▶️ preview link
  in about 15–20 minutes. No plan comment first; the PR says what it assumed.
- The dashboard puts ⚡ next to quick-lane issues, and shows their slot as "quick 1/1"
  beside the build slots.
- When the time box or cap is hit, the PR says what wasn't verified.

## Alternatives considered
- **The worker self-assigns a slot without the orchestrator.** It's faster by about a minute,
  but the worker can't see other issues safely, so two workers could race. The ping-triggered
  tick is nearly as fast.
- **Quick issues bypass slots entirely.** On bay-area-pirates, several quick spawns at once
  would all edit the same spawn list, and adjacent-line edits do conflict.
- **A new `QUICK` directive at dispatch.** The orchestrator would have to classify issues
  before the worker has read the code. The worker's triage is the better judge, and the label
  covers what the owner already knows.
- **`claude:quick` as the label name.** It reads more consistently, but every `claude:` label
  is treated as a phase by `watch-new.py`, `gather.py` and old workers, which would strip or
  misread it.

## Out of scope
- Making dispatch itself cheaper (pre-warmed sessions).
- Auto-merging quick-lane PRs.
- The orchestrator nudging workers whose progress is stale (the dashboard already flags
  those as stalled).

## Plan
- `.claude/skills/work-issue/SKILL.md`: Triage (lane criteria, `lane:quick`, background
  install, skip the plan comment), State (`lane` field, progress rules), Build (scoped
  play-check, time box, local check during deploy, cap, leaving the lane).
- `.claude/skills/tick/SKILL.md`: the quick slot in "Build slots and dispatch", and quick
  candidates handled first on a ping.
- `bot.config.yml`: `quick_lane: true`, `quick_slots: 1`, `quick_cap_minutes: 25` in
  `defaults` only, with comments.
- `bin/dashboard.py`: ⚡ marker, quick-slot count, and quick holders not counted against
  `build_slots`.
- `bin/gather.py`: nothing (the label is already in `labels`).
- `CLAUDE.md`, `README.md`, `bot-setup` label table: document `lane:quick`.
- Verify: `py_compile`; run `bin/dashboard.py` read-only against the live snapshot and
  against a hand-edited one that has a quick-lane issue; diff the playbook version.

## Backwards compatibility
- Old workers never write `lane`, so they are normal-lane and keep using `build_slots`.
- The new playbook's triage only adds steps, and no new message words are added
  (`BUILD` still starts the build).
- The orchestrator change ships with the playbook. A worker on the old playbook that
  reaches `approved` simply gets a normal slot.
