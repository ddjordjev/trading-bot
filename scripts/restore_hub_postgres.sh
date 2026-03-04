#!/bin/bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <backup-file.sqlc>"
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_FILE="$1"
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"

docker compose -f "$ROOT_DIR/docker-compose.yml" exec -T bot-hub-postgres \
  pg_restore -U "${HUB_POSTGRES_USER:-tradeborg}" -d "${HUB_POSTGRES_DB:-trading_db}" --clean --if-exists < "$BACKUP_FILE"

echo "Postgres restore complete from: $BACKUP_FILE"
