# Design: size estimates and a small-issue build slot (#3)

## Goal
Stop large issues from filling every build slot while quick ones pile up: workers
estimate size, and the tick keeps one slot for small/medium work when there are 2+ slots.

## Estimate (worker playbook)
- New status JSON field, written by the worker:
  `"estimate": {"size": "S|M|L", "minutes": N, "why": "one line", "at": "<UTC>"}`
  - `minutes` = expected worker time from `BUILD` to "PR ready" (implement, test,
    preview, play-check), not owner review time.
  - Sizes: **S** ≤ 15 min, **M** 15–30, **L** > 30 (the issue's "30+ minutes").
- First written in Triage (so issues still in design already have one), refined
  when entering `approved` (fast path or after design). Revised during Build if it
  turns out much bigger, same as footprint ("found 2 more call sites").
- Optional and additive: older workers just omit it.

## Scheduling (tick step 3)
- `build_slots: 1` → unchanged, strict queue order.
- `build_slots >= 2` and `small_slot: true` (new setting, default on):
  the **last free slot** is not given to an **L** candidate while a non-L issue is
  *waiting* — approved (and parallel-safe), or in `designing` (worker is actively
  heading for `approved`). Instead the first non-L approved candidate in queue
  order gets it; if none is approved yet, the slot stays free for now.
  - Slots other than the last are handed out exactly as today (queue order +
    parallelism check), so L issues still get built.
  - `needs-input` issues don't hold a slot back (they can wait on the owner for hours).
  - `claude:next` on an L issue overrides the reservation (the owner asked for it).
- **In-review holders:** they occupy a slot like today, whatever their size; the
  rule only looks at free slots, so an L PR sitting in review doesn't make the
  rule stricter or looser. (Whether in-review should hold a slot at all is a
  separate question — out of scope here.)
- **Missing estimate** (workers on playbook 73cad609fe, or orchestrator-era
  issues): the parallelism subagent, which already reads each candidate's plan
  and footprint, also returns `size`/`minutes` for candidates and holders without
  one; the tick records it as `estimate` with `"by": "orchestrator"`. If that
  fails, unknown = **M** (never held back, never counted as large).

## Dashboard
- Issue cell shows the size, e.g. `#12 Add jump · L ~45m`.
- Notes for a held-back L candidate: `waiting: last slot kept for small issues (#15, #18)`.
- dashboard-data JSON: each issue gets `"estimate": {"size","minutes","by"} | null`
  (additive; factory ignores unknown fields).

## Files
- `.claude/skills/work-issue/SKILL.md` — estimate in Triage/approved/Build, status example.
- `.claude/skills/tick/SKILL.md` — step 3 rule, subagent prompt returns sizes.
- `bot.config.yml` — `small_slot: true` under defaults, with a comment.
- `bin/dashboard.py` — size in the issue cell, held-back note, `estimate` in data.
- `README.md` — one paragraph on the small-issue slot.

## Verification
`python3 -m py_compile bin/*.py`; `bin/dashboard.py` (read-only) on a live
snapshot and on a doctored snapshot with L/S approved issues to show the note and
data field; `bin/playbook.py --version` changes.
