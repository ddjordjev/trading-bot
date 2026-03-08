#!/usr/bin/env bash
set -u

export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR" || exit 1
DC=(docker compose --env-file .env --env-file env/local.compose.env)

if [ ! -f .env ] || [ ! -f env/local.compose.env ]; then
  echo "ERROR: missing .env or env/local.compose.env for local watchdog runtime" >&2
  exit 1
fi
if ! rg -q '^RUNTIME_ENV_OVERRIDE_FILE=' env/local.compose.env || ! rg -q '^RUNTIME_SECRETS_FILE=' env/local.compose.env; then
  echo "ERROR: env/local.compose.env missing runtime file pointers" >&2
  exit 1
fi

HOST_DATA_DIR="/Users/damirdjordjev/workspace/trading-bot-data"
ACTION_LOG="$ROOT_DIR/logs/night-watch-actions.log"
mkdir -p "$ROOT_DIR/logs"

last_redeploy_epoch=0
redeploy_cooldown_secs=300
heartbeat_interval_secs="${NIGHT_WATCH_HEARTBEAT_SECS:-10800}" # default: every 3h
last_heartbeat_epoch=0

log() {
  printf '[%s] %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$1" | tee -a "$ACTION_LOG"
}

full_redeploy() {
  log "redeploy:start"
  "${DC[@]}" down
  find "$HOST_DATA_DIR" \( -name "*.json" -o -name "*.lock" -o -name "activate" -o -name "STOP" -o -name "CLOSE_ALL" \) | xargs rm -f
  "${DC[@]}" build
  "${DC[@]}" up -d
  log "redeploy:done"
}

emit_heartbeat() {
  local ps_out healthy_count unhealthy_count exited_count restarting_count running_count
  ps_out="$("${DC[@]}" ps 2>&1)"
  healthy_count="$(printf "%s\n" "$ps_out" | rg -c "healthy" || true)"
  unhealthy_count="$(printf "%s\n" "$ps_out" | rg -c "unhealthy" || true)"
  exited_count="$(printf "%s\n" "$ps_out" | rg -c "Exit|Exited|dead" || true)"
  restarting_count="$(printf "%s\n" "$ps_out" | rg -c "Restarting" || true)"
  running_count="$(printf "%s\n" "$ps_out" | rg -c " Up " || true)"
  log "heartbeat: running=${running_count} healthy=${healthy_count} unhealthy=${unhealthy_count} exited=${exited_count} restarting=${restarting_count}"
}

log "watchdog:start"
while true; do
  now_epoch="$(date +%s)"
  since_heartbeat=$((now_epoch - last_heartbeat_epoch))
  if [ "$since_heartbeat" -ge "$heartbeat_interval_secs" ]; then
    emit_heartbeat
    last_heartbeat_epoch="$now_epoch"
  fi

  ps_out="$("${DC[@]}" ps 2>&1)"
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
