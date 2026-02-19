#!/usr/bin/env bash
set -euo pipefail

# Trading Bot — Session Runner
# Usage: ./scripts/run_session.sh [start|stop|status|logs|rebuild|preflight]

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[BOT]${NC} $1"; }
warn() { echo -e "${YELLOW}[BOT]${NC} $1"; }
err()  { echo -e "${RED}[BOT]${NC} $1"; }

cmd_preflight() {
    log "Running preflight checks..."
    python scripts/preflight_check.py
}

cmd_build() {
    log "Building Docker images..."
    docker compose build
    log "Build complete."
}

cmd_start() {
    log "Starting all services..."
    docker compose up -d
    sleep 3
    docker compose ps
    log "Dashboard: http://localhost:${DASHBOARD_PORT:-9035}"
    log "Exchange:  $(grep EXCHANGE_PLATFORM_URL .env | cut -d= -f2-)"
    if [ -z "$(grep EXCHANGE_PLATFORM_URL .env | cut -d= -f2-)" ]; then
        log "Exchange:  https://demo.binance.com/en/futures (auto-detected)"
    fi
}

cmd_stop() {
    warn "Stopping all services..."
    docker compose down
    log "All services stopped."
}

cmd_status() {
    log "Service status:"
    docker compose ps
    echo ""
    log "Recent bot log:"
    docker compose logs --tail 10 trading-bot 2>/dev/null || warn "No logs yet"
    echo ""
    log "Trade count:"
    docker compose exec -T trading-bot python -c "
from db import TradeDB
db = TradeDB(); db.connect()
print(f'  Trades logged: {db.trade_count()}')
" 2>/dev/null || warn "Could not read trade DB"
}

cmd_logs() {
    local service="${1:-trading-bot}"
    docker compose logs -f "$service"
}

cmd_rebuild() {
    warn "Rebuilding and restarting..."
    docker compose build
    docker compose up -d
    sleep 3
    docker compose ps
    log "Rebuild complete."
}

cmd_snapshot() {
    local ts=$(date -u +"%Y-%m-%d_%H%M")
    local file="docs/reports/snapshot_${ts}.md"
    log "Taking snapshot → $file"

    cat > "$file" << SNAP
# Snapshot — $ts UTC

## Service Status
\`\`\`
$(docker compose ps 2>/dev/null || echo "Docker not running")
\`\`\`

## Recent Logs (last 30 lines)
\`\`\`
$(docker compose logs --tail 30 trading-bot 2>/dev/null || echo "No logs")
\`\`\`

## Trade Database
\`\`\`
$(docker compose exec -T trading-bot python -c "
from db import TradeDB
db = TradeDB(); db.connect()
print(f'Total trades: {db.trade_count()}')
for t in db.get_recent(10):
    print(f'  {t.timestamp} {t.symbol} {t.side} {t.action} PnL:{t.pnl:+.2f}')
" 2>/dev/null || echo "Could not read DB")
\`\`\`
SNAP
    log "Snapshot saved to $file"
}

case "${1:-help}" in
    preflight) cmd_preflight ;;
    build)     cmd_build ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    status)    cmd_status ;;
    logs)      cmd_logs "${2:-}" ;;
    rebuild)   cmd_rebuild ;;
    snapshot)  cmd_snapshot ;;
    help|*)
        echo "Usage: $0 {preflight|build|start|stop|status|logs|rebuild|snapshot}"
        echo ""
        echo "  preflight  — Run pre-flight checks (API keys, connectivity)"
        echo "  build      — Build Docker images"
        echo "  start      — Start all services (docker compose up -d)"
        echo "  stop       — Stop all services"
        echo "  status     — Show service health, recent logs, trade count"
        echo "  logs [svc] — Tail logs (default: trading-bot)"
        echo "  rebuild    — Rebuild images and restart"
        echo "  snapshot   — Save current state to docs/reports/"
        ;;
esac
