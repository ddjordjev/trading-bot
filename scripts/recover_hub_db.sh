#!/bin/bash
# Recover hub.db when corrupted (sqlite3 "file is not a database").
# Validates hub.db, restores from backup if invalid, then restarts the hub.
# Run from host: ./scripts/recover_hub_db.sh

set -e

DATA_DIR="${HOST_DATA_DIR:-/Users/damirdjordjev/workspace/trading-bot-data}"
BACKUP_DIR="${BACKUP_DIR:-/Users/damirdjordjev/workspace/trading-bot-backups}"
HUB_DB="$DATA_DIR/hub.db"

export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"

if [ ! -f "$HUB_DB" ]; then
  echo "hub.db not found at $HUB_DB"
  exit 1
fi

# Validate: run SQLite integrity check
if ! sqlite3 "$HUB_DB" "PRAGMA quick_check;" 2>/dev/null | grep -q "ok"; then
  echo "hub.db is corrupted or invalid (sqlite3: file is not a database)"
  CORRUPTED=1
fi

if [ -n "$CORRUPTED" ]; then
  BACKUP="$BACKUP_DIR/hub.db"
  BACKUP_VALID=0
  if [ -f "$BACKUP" ] && sqlite3 "$BACKUP" "PRAGMA quick_check;" 2>/dev/null | grep -q "ok"; then
    BACKUP_VALID=1
  fi

  if [ "$BACKUP_VALID" = 1 ]; then
    echo "Restoring hub.db from valid backup: $BACKUP"
    cp "$BACKUP" "$HUB_DB"
  else
    echo "No valid backup (missing or corrupted). Creating fresh hub.db (trade history lost)."
    rm -f "$HUB_DB" "$HUB_DB-shm" "$HUB_DB-wal"
    # Hub creates empty DB with schema on startup
  fi

  echo "Restarting hub..."
  cd "$(dirname "$0")/.."
  docker compose restart bot-hub
  echo "Done. Check dashboard in ~30s."
else
  echo "hub.db is valid. No recovery needed."
fi
