# Design: #24 Cloudflare not consistently used for previews

## What's happening

1. **Workers keep the settings they were dispatched with.** The brief (project
   settings, `preview` included) and the pinned playbook are fixed when a
   worker session starts. PR #23 switched minefield-commander and
   bay-area-pirates to `preview: cloudflare` at ~10:35 UTC; workers started
   before that keep `preview: pages` and a playbook that has no `cloudflare`
   mode at all, so every later push still goes to the `previews` branch /
   github.io. Today exactly one live issue is affected: bay-area-pirates#507
   (PR #509, dispatched ~09:24 UTC) →
   https://briansteffens.github.io/bay-area-pirates/previews/pr-509/.
   Every worker dispatched after the switch uses Cloudflare.
2. **`pr-<n>` means different numbers to different workers.** The playbook
   says `--branch pr-<n>`, and `<n>` is the issue number everywhere else in
   it, but `pr-` reads like the PR number. A fast-path build publishes its
   preview *before* its PR exists, so it can only use the issue number;
   design-PR workers sometimes use the PR number (#519 → `pr-521`). Cleanup
   (Merged step 3, tick's closed-without-merge row) rebuilds the alias from
   `<n>`, so a worker that used the other number leaks its deployments.

## Proposal

### A. One number: the issue number
- Playbook: say explicitly that `pr-<n>` uses the **issue** number (always
  known, even before a fast-path PR exists), for `cloudflare` and `pages`.
  Keep the `pr-` prefix so existing aliases, links and cleanup keep working.
- Cleanup (playbook Merged step 3, tick closed-without-merge row): take the
  alias from the recorded `preview` URL (`https://<alias>.<project>.pages.dev/`
  or `.../previews/<folder>/`) instead of recomputing it, so previews made by
  either convention get deleted.

### B. Notice and fix workers on a stale preview mode
- `bin/plan.py`: new `stale_preview` list: in-flight issues whose `preview`
  URL doesn't match the project's current mode (a github.io/previews URL
  under `cloudflare`, a pages.dev URL under `pages`, either under `artifact`
  etc.). Self-test cases added.
- `bin/dashboard.py`: a ⚠️ line for those issues ("preview on old mode").
- Tick skill: for each `stale_preview` issue that's waiting on the owner
  (`in-review` ready / `needs-input`) — i.e. not mid-build — redispatch it:
  `bin/dispatch.py prepare ... --redispatch --directive RESUME` with a note
  that the preview mode changed. The fresh worker gets the current brief and
  playbook, re-reads everything, rebuilds and republishes the preview in the
  new mode, posts "Revision n ready" with the new link. The old session is
  sent `HANDOFF` first (it stops without touching status or labels).
- Old-mode leftovers (e.g. `previews/pr-509/`) are deleted by the new worker
  after the new preview is live.

## Out of scope
- Changing the alias prefix (`pr-` → `issue-`).
- Re-briefing running workers in place (peer messages are one-word directives
  only; older playbooks don't know new words).

## Backwards compatibility
- Workers on older playbooks keep working; they just don't get the wording
  fix. Cleanup reading the alias from `preview` handles both conventions.
- The redispatch only touches issues whose recorded preview disagrees with the
  config, which after this round is none until the next mode change.
