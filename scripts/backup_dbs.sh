#!/bin/bash
# Daily local backup of all SQLite databases from the host data directory.
# Overwrites previous backup — only keeps the latest copy.
# Run from the host: ./scripts/backup_dbs.sh

DATA_DIR="${HOST_DATA_DIR:-/Users/damirdjordjev/workspace/trading-bot-data}"
BACKUP_DIR="${BACKUP_DIR:-/Users/damirdjordjev/workspace/trading-bot-backups}"

mkdir -p "$BACKUP_DIR"

if [ ! -d "$DATA_DIR" ]; then
    echo "Data directory not found: $DATA_DIR"
    exit 1
fi

dbs=$(find "$DATA_DIR" -name "*.db" 2>/dev/null)

if [ -z "$dbs" ]; then
    echo "No databases found in $DATA_DIR"
    exit 1
fi

count=0
for db in $dbs; do
    rel="${db#$DATA_DIR/}"
    dest="$BACKUP_DIR/$rel"
    mkdir -p "$(dirname "$dest")"
    cp "$db" "$dest" 2>/dev/null && count=$((count + 1))
done

echo "$(date '+%Y-%m-%d %H:%M:%S') Backed up $count databases to $BACKUP_DIR"
