#!/usr/bin/env bash
set -u

export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR" || exit 1

HOST_DATA_DIR="/Users/damirdjordjev/workspace/trading-bot-data"
ACTION_LOG="$ROOT_DIR/logs/night-watch-actions.log"
mkdir -p "$ROOT_DIR/logs"

last_redeploy_epoch=0
redeploy_cooldown_secs=300

log() {
  printf '[%s] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$1" | tee -a "$ACTION_LOG"
}

full_redeploy() {
  log "redeploy:start"
  docker compose down
  find "$HOST_DATA_DIR" \( -name "*.json" -o -name "*.lock" -o -name "activate" -o -name "STOP" -o -name "CLOSE_ALL" \) | xargs rm -f
  docker compose build
  docker compose up -d
  log "redeploy:done"
}

log "watchdog:start"
while true; do
  ps_out="$(docker compose ps 2>&1)"
  if printf "%s\n" "$ps_out" | rg -q "unhealthy|Exit|dead|Restarting"; then
    now_epoch="$(date +%s)"
    since_last=$((now_epoch - last_redeploy_epoch))
    if [ "$since_last" -ge "$redeploy_cooldown_secs" ]; then
      log "watchdog:detected_unhealthy_or_exited"
      full_redeploy
      last_redeploy_epoch="$(date +%s)"
    else
      log "watchdog:issue_detected_but_in_cooldown"
    fi
  fi
  sleep 10
done
