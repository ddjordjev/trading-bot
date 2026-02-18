#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FRONTEND_DIR="$PROJECT_DIR/web/frontend"

echo "==> Installing frontend dependencies..."
cd "$FRONTEND_DIR"
npm install

echo "==> Building frontend..."
npm run build

echo "==> Done! Built files in $FRONTEND_DIR/dist/"
echo "    Start the bot with: python bot.py"
echo "    Dashboard will be at http://localhost:8080"
