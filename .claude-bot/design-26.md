# Design: flicker check in the worker play-check (#26)

## Goal
When a worker changes 3D geometry or how models move against each other, its
play-check actively hunts for flicker (z-fighting, depth-precision shimmer,
shadow acne) by moving the camera / stepping the animation in tiny increments
and comparing frames, and fixes what its change caused before asking for review.

## Approach
- **Playbook rule** (work-issue, Build step 5, next to "Scope the play-check to
  the diff"): a *flicker check* is required when the diff adds or changes
  meshes, materials, decals/overlays, camera near/far, shadows, or anything that
  moves one model against another (doors, vehicles docking at platforms). Skip
  it for non-3D projects and for diffs that don't touch rendering.
- **Method** (what the rule tells the worker to do):
  1. Freeze game time and randomness (pause the loop / fixed timestep) so the
     only change between frames is the one we make.
  2. For each spot the diff touches (and each moving-part interaction it adds),
     capture ~30 frames while either (a) nudging the camera/character along a
     slow straight line, a fraction of a pixel per frame, or (b) stepping the
     animation (door open → close, car pulling in) in small time steps.
  3. Score **oscillation**: a pixel that goes A → B → A across three
     consecutive frames while the motion is monotonic. Smooth motion almost
     never does that; z-fighting does it constantly, in speckles and stripes.
     Report the % of oscillating pixels per frame plus a heat-map PNG.
  4. Above a small threshold → look at the heat map, fix (offset/polygonOffset,
     remove coplanar faces, separate the surfaces, tighten near/far), re-run.
- **Shared helper** `bin/flicker-check.mjs` in this repo: Playwright + a
  dependency-free PNG diff. The worker passes the page URL and a small
  per-project "driver" (JS that pauses time, places the camera, steps it).
  Workers fetch it from the same pinned commit as the playbook.
- **PR "How I verified it"** gets a flicker line: spots checked, score, and
  heat maps for anything found (fixed or pre-existing).

## What the owner sees
A "flicker: checked N spots, 0.0x% oscillating" line in each relevant PR, and
fewer flickering doors/platforms in the previews.

## Alternatives considered
- Compare two frames of the same pose: deterministic rendering makes them
  identical, so it misses z-fighting, which shows up under motion.
- Plain consecutive-frame diff: every moving edge lights up; oscillation
  filters that out.
- Prose-only rule, each worker writes its own checker: inconsistent results,
  and time re-spent every build.

## Out of scope
- Sweeping and fixing the flicker that's already in bay-area-pirates (BART
  doors, streetcar): suggest a separate issue there, which the new check would
  then serve.
- CI integration in game repos.

## Plan
- `.claude/skills/work-issue/SKILL.md`: the rule + method above (~15 lines).
- `bin/flicker-check.mjs`: helper; verify on a tiny three.js page with two
  coplanar planes (must flag) and one with an offset (must pass).
- README: one line under play-checks.
- Playbook version changes; running workers keep their old one (compatible).

## Decisions (owner, rev 1)
1. Shared `bin/flicker-check.mjs` in this repo, fetched by workers from the same
   pinned commit as the playbook.
2. Flicker the diff didn't cause: report it in PR Notes with the heat map, don't fix.
3. Quick lane: run it, inside the 10-minute play-check box.
