#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
BACKUP_ENV="$ROOT_DIR/.env.backup.pre_pg_cutover"
ROLLBACK_SNAPSHOT="$ROOT_DIR/.env.rollback.postgres.failed"

export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"

if [ ! -f "$ENV_FILE" ]; then
  echo ".env not found at $ENV_FILE"
  exit 1
fi

cp "$ENV_FILE" "$BACKUP_ENV"
echo "Backed up .env -> $BACKUP_ENV"

rollback() {
  echo "Cutover failed; rolling back to previous env..."
  cp "$BACKUP_ENV" "$ENV_FILE"
  cp "$ENV_FILE" "$ROLLBACK_SNAPSHOT"
  docker compose -f "$ROOT_DIR/docker-compose.yml" up -d
  echo "Rollback completed."
}

trap rollback ERR

echo "Stopping trading services for cutover..."
docker compose -f "$ROOT_DIR/docker-compose.yml" stop bot-hub bot-momentum bot-indicators bot-meanrev bot-swing bot-extreme bot-scalper bot-fullstack bot-conservative bot-aggressive bot-hedger

echo "Ensuring postgres service is up..."
docker compose -f "$ROOT_DIR/docker-compose.yml" up -d bot-hub-postgres

echo "Applying postgres schema..."
"$ROOT_DIR/scripts/migrate_hub_postgres_schema.sh"

echo "Migrating sqlite -> postgres..."
HUB_POSTGRES_DSN="${HUB_POSTGRES_DSN:-postgresql://tradeborg:tradeborg@localhost:${HUB_POSTGRES_PORT:-5438}/${HUB_POSTGRES_DB:-trading_db}}" \
SQLITE_HUB_DB_PATH="${SQLITE_HUB_DB_PATH:-$ROOT_DIR/data/hub.db}" \
  "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/migrate_hubdb_to_postgres.py"

echo "Switching backend to postgres in .env..."
if grep -q "^HUB_DB_BACKEND=" "$ENV_FILE"; then
  sed -i '' 's/^HUB_DB_BACKEND=.*/HUB_DB_BACKEND=postgres/' "$ENV_FILE"
else
  echo "HUB_DB_BACKEND=postgres" >> "$ENV_FILE"
fi

echo "Starting full stack..."
docker compose -f "$ROOT_DIR/docker-compose.yml" up -d

echo "Smoke checks..."
curl -sf "http://localhost:${DASHBOARD_PORT:-9035}/health" >/dev/null
echo "health: ok"

trap - ERR
echo "Cutover complete."
