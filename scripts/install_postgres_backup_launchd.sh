#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_PATH="${HOME}/Library/LaunchAgents/com.tradingbot.postgres-backup.plist"
BACKUP_DIR="${BACKUP_DIR:-/Users/damirdjordjev/workspace/trading-bot-backups/postgres}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-21600}" # 6 hours
RETENTION_DAYS="${RETENTION_DAYS:-2}"

mkdir -p "${HOME}/Library/LaunchAgents" "$BACKUP_DIR"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.tradingbot.postgres-backup</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd "$ROOT_DIR" && RETENTION_DAYS=$RETENTION_DAYS ./scripts/backup_hub_postgres.sh</string>
  </array>
  <key>StartInterval</key>
  <integer>$INTERVAL_SECONDS</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$BACKUP_DIR/backup.log</string>
  <key>StandardErrorPath</key>
  <string>$BACKUP_DIR/backup.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load "$PLIST_PATH"

echo "Installed launchd job: $PLIST_PATH"
echo "Schedule interval (seconds): $INTERVAL_SECONDS"
echo "Backup directory: $BACKUP_DIR"
