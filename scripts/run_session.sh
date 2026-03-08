#!/usr/bin/env bash
set -euo pipefail

# Trading Bot — Session Runner
# Usage: ./scripts/run_session.sh [start|stop|status|logs|rebuild|preflight|openclaw-preflight]

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DC=(docker compose --env-file .env --env-file env/local.compose.env)

require_local_compose_env() {
    if [ ! -f .env ]; then
        err "Missing .env in project root."
        exit 1
    fi
    if [ ! -f env/local.compose.env ]; then
        err "Missing env/local.compose.env."
        exit 1
    fi
    if ! rg -q '^RUNTIME_ENV_OVERRIDE_FILE=' env/local.compose.env; then
        err "env/local.compose.env is missing RUNTIME_ENV_OVERRIDE_FILE."
        exit 1
    fi
    if ! rg -q '^RUNTIME_SECRETS_FILE=' env/local.compose.env; then
        err "env/local.compose.env is missing RUNTIME_SECRETS_FILE."
        exit 1
    fi
}

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[BOT]${NC} $1"; }
warn() { echo -e "${YELLOW}[BOT]${NC} $1"; }
err()  { echo -e "${RED}[BOT]${NC} $1"; }

cmd_preflight() {
    log "Running preflight checks..."
    "$ROOT/.venv/bin/python" scripts/preflight_check.py
}

cmd_openclaw_preflight() {
    log "Running OpenClaw preflight..."
    "$ROOT/scripts/openclaw_preflight.sh"
}

cmd_materialize_local_secrets() {
    log "Materializing local runtime secrets..."
    "$ROOT/scripts/materialize_runtime_secrets.sh" local
}

cmd_build() {
    require_local_compose_env
    cmd_materialize_local_secrets
    log "Building Docker images..."
    "${DC[@]}" build
    log "Build complete."
}

cmd_start() {
    require_local_compose_env
    cmd_materialize_local_secrets
    log "Starting all services..."
    "${DC[@]}" up -d
    sleep 3
    "${DC[@]}" ps
    log "Dashboard: http://localhost:${DASHBOARD_PORT:-9035}"
    local exchange_url=""
    if [ -f .env ]; then
        exchange_url="$(grep -E '^EXCHANGE_PLATFORM_URL=' .env | head -n1 | cut -d= -f2- || true)"
    fi
    if [ -n "$exchange_url" ]; then
        log "Exchange:  $exchange_url"
    else
        log "Exchange:  https://demo.binance.com/en/futures (auto-detected)"
    fi
}

cmd_stop() {
    require_local_compose_env
    warn "Stopping all services..."
    "${DC[@]}" down
    log "All services stopped."
}

cmd_status() {
    require_local_compose_env
    log "Service status:"
    "${DC[@]}" ps
    echo ""
    log "Recent hub log:"
    "${DC[@]}" logs --tail 10 bot-hub 2>/dev/null || warn "No logs yet"
    echo ""
    log "Trade count:"
    "${DC[@]}" exec -T bot-hub python -c "
from db.hub_repository import make_hub_repository
db = make_hub_repository(); db.connect()
print(f'  Trades logged: {db.trade_count()}')
" 2>/dev/null || warn "Could not read trade DB"
}

cmd_logs() {
    require_local_compose_env
    local service="${1:-bot-hub}"
    "${DC[@]}" logs -f "$service"
}

cmd_rebuild() {
    require_local_compose_env
    warn "Rebuilding and restarting..."
    cmd_materialize_local_secrets
    "${DC[@]}" build
    "${DC[@]}" up -d
    sleep 3
    "${DC[@]}" ps
    log "Rebuild complete."
}

cmd_snapshot() {
    require_local_compose_env
    local ts=$(date -u +"%Y-%m-%d_%H%M")
    local file="docs/reports/snapshot_${ts}.md"
    log "Taking snapshot → $file"
    mkdir -p "$(dirname "$file")"

    cat > "$file" << SNAP
# Snapshot — $ts UTC

## Service Status
\`\`\`
$("${DC[@]}" ps 2>/dev/null || echo "Docker not running")
\`\`\`

## Recent Logs (last 30 lines)
\`\`\`
$("${DC[@]}" logs --tail 30 bot-hub 2>/dev/null || echo "No logs")
\`\`\`

## Trade Database
\`\`\`
$("${DC[@]}" exec -T bot-hub python -c "
from db.hub_repository import make_hub_repository
db = make_hub_repository(); db.connect()
print(f'Total trades: {db.trade_count()}')
for t in db.get_all_trades(10):
    print(f'  {t.closed_at} {t.symbol} {t.side} {t.action} PnL:{t.pnl_usd:+.2f}')
" 2>/dev/null || echo "Could not read DB")
\`\`\`
SNAP
    log "Snapshot saved to $file"
}

case "${1:-help}" in
    preflight) cmd_preflight ;;
    openclaw-preflight) cmd_openclaw_preflight ;;
    build)     cmd_build ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    status)    cmd_status ;;
    logs)      cmd_logs "${2:-}" ;;
    rebuild)   cmd_rebuild ;;
    snapshot)  cmd_snapshot ;;
    sync-secrets)
        if ! command -v gh &>/dev/null; then
            echo "ERROR: gh CLI not installed"; exit 1
        fi
        count=0
        while IFS='=' read -r key value; do
            [[ -z "$key" || "$key" == \#* ]] && continue
            case "$key" in
                *_API_KEY|*_API_SECRET)
                    gh secret set "$key" --body "$value"
                    echo "  ✓ $key"
                    ((count++))
                    ;;
            esac
        done < "$ROOT/.env"
        echo "Synced $count exchange API secrets from .env → GitHub"
        ;;
    help|*)
        echo "Usage: $0 {preflight|openclaw-preflight|build|start|stop|status|logs|rebuild|snapshot|sync-secrets}"
        echo ""
        echo "  preflight    — Run pre-flight checks (API keys, connectivity)"
        echo "  openclaw-preflight — Validate OpenClaw endpoint + hub integration surfaces"
        echo "  build        — Build Docker images"
        echo "  start        — Start all services (docker compose up -d)"
        echo "  stop         — Stop all services"
        echo "  status       — Show service health, recent logs, trade count"
        echo "  logs [svc]   — Tail logs (default: bot-hub)"
        echo "  rebuild      — Rebuild images and restart"
        echo "  snapshot     — Save current state to docs/reports/"
        echo "  sync-secrets — Sync exchange API secrets from .env to GitHub"
        ;;
esac
