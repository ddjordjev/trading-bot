#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/Users/damirdjordjev/workspace/trading-bot-backups/postgres}"
RETENTION_DAYS="${RETENTION_DAYS:-2}"
STAMP="$(date -u +%Y%m%d_%H%M%S)"
OUT_FILE="$BACKUP_DIR/trading_db_${STAMP}.sqlc"

mkdir -p "$BACKUP_DIR"
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"

docker compose -f "$ROOT_DIR/docker-compose.yml" exec -T bot-hub-postgres \
  pg_dump -U "${HUB_POSTGRES_USER:-tradeborg}" -d "${HUB_POSTGRES_DB:-trading_db}" -F c > "$OUT_FILE"

# Basic backup integrity check: list archive contents.
docker compose -f "$ROOT_DIR/docker-compose.yml" exec -T bot-hub-postgres \
  sh -lc 'tmp="$(mktemp)"; cat > "$tmp"; pg_restore -l "$tmp" >/dev/null; rm -f "$tmp"' < "$OUT_FILE"

# Rolling retention window (default: 2 days)
find "$BACKUP_DIR" -name "trading_db_*.sqlc" -type f -mtime +"$RETENTION_DAYS" -delete

echo "Postgres backup written: $OUT_FILE"
