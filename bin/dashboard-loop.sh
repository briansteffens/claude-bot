#!/usr/bin/env bash
# Keeps the dashboard fresh independently of the orchestrator's turns: every
# DASHBOARD_INTERVAL seconds (default 60) it re-renders the dashboard from
# GitHub and updates the issue if anything changed.
#
#   bin/dashboard-loop.sh --ensure   start it in the background unless running
#   bin/dashboard-loop.sh --status   say whether it's running, show the log tail
#   bin/dashboard-loop.sh --stop     stop it
#   bin/dashboard-loop.sh            run in the foreground
#
# The container can be recycled while the session is idle, which kills the
# loop; /tick runs --ensure at the start of every turn to bring it back.
set -uo pipefail
cd "$(dirname "$0")/.."

STATE_DIR="${HOME}/.cache/claude-bot"
PID_FILE="$STATE_DIR/dashboard-loop.pid"
LOG_FILE="$STATE_DIR/dashboard-loop.log"
INTERVAL="${DASHBOARD_INTERVAL:-60}"
mkdir -p "$STATE_DIR"

running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

case "${1:-}" in
  --ensure)
    if running; then
      echo "dashboard loop running (pid $(cat "$PID_FILE"))"
    else
      setsid nohup "$0" >>"$LOG_FILE" 2>&1 </dev/null &
      sleep 1
      echo "dashboard loop started (pid $(cat "$PID_FILE" 2>/dev/null || echo '?'))"
    fi
    exit 0
    ;;
  --status)
    if running; then echo "running (pid $(cat "$PID_FILE"))"; else echo "not running"; fi
    tail -n 5 "$LOG_FILE" 2>/dev/null
    exit 0
    ;;
  --stop)
    running && kill "$(cat "$PID_FILE")" && echo "stopped"
    rm -f "$PID_FILE"
    exit 0
    ;;
esac

if running && [ "$(cat "$PID_FILE")" != "$$" ]; then
  echo "already running (pid $(cat "$PID_FILE"))"
  exit 0
fi
echo $$ >"$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT

while true; do
  # Keep the scripts and config current; the orchestrator also pulls each tick.
  git pull -q --ff-only 2>/dev/null
  echo "$(date -u +%H:%M:%S) $(bin/dashboard.py --write 2>&1 | tail -n 1)"
  # Keep the log small.
  if [ "$(wc -l <"$LOG_FILE" 2>/dev/null || echo 0)" -gt 2000 ]; then
    tail -n 500 "$LOG_FILE" >"$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
  fi
  sleep "$INTERVAL"
done
