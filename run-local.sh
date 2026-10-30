#!/usr/bin/env bash
# Run the claude-bot orchestrator on your own machine, inside tmux so it keeps
# going after you close the terminal. Attach any time with:
#   tmux attach -t claude-bot
# Extra arguments are passed to `claude` (e.g. --permission-mode acceptEdits).
#
# Local mode works inline: this one session designs, builds and opens PRs itself.
set -euo pipefail
cd "$(dirname "$0")"

for cmd in claude gh tmux; do
  command -v "$cmd" >/dev/null || { echo "missing: $cmd" >&2; exit 1; }
done
gh auth status >/dev/null || { echo "run: gh auth login" >&2; exit 1; }

if tmux has-session -t claude-bot 2>/dev/null; then
  echo "already running: tmux attach -t claude-bot"
  exit 0
fi

interval="${CLAUDE_BOT_INTERVAL:-10m}"
args=""
[ $# -gt 0 ] && args="$(printf '%q ' "$@")"
tmux new-session -d -s claude-bot -c "$PWD" \
  "claude $args'/loop $interval /tick'"
echo "started: tmux attach -t claude-bot"
