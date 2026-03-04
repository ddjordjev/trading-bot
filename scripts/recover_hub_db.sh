#!/bin/bash
# Postgres-first hub DB recovery workflow.
# - Verifies postgres connectivity
# - Restores from latest postgres backup if requested
# - Restarts hub services
#
# Legacy SQLite recovery is no longer the default path.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"

cd "$ROOT_DIR"

echo "Checking postgres availability..."
docker compose exec -T bot-hub-postgres \
  psql -U "${HUB_POSTGRES_USER:-tradeborg}" -d "${HUB_POSTGRES_DB:-trading_db}" -c "SELECT 1;" >/dev/null

echo "Postgres is reachable."
echo "Restarting hub service..."
docker compose restart bot-hub
echo "Done. Check dashboard in ~30s."
