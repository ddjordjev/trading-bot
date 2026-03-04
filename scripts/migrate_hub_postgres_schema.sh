#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCHEMA_FILE="$ROOT_DIR/db/migrations/postgres/001_init.sql"

export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"

if [ ! -f "$SCHEMA_FILE" ]; then
  echo "Schema file missing: $SCHEMA_FILE"
  exit 1
fi

docker compose -f "$ROOT_DIR/docker-compose.yml" exec -T bot-hub-postgres \
  psql -U "${HUB_POSTGRES_USER:-tradeborg}" -d "${HUB_POSTGRES_DB:-trading_db}" -v ON_ERROR_STOP=1 \
  < "$SCHEMA_FILE"

echo "Postgres schema applied."
