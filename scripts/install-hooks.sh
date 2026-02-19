#!/usr/bin/env bash
set -euo pipefail

HOOK_DIR="$(git rev-parse --git-dir)/hooks"
mkdir -p "$HOOK_DIR"

cat > "$HOOK_DIR/pre-push" << 'HOOK'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
ENV_FILE="$ROOT/.env"
HASH_FILE="$(git rev-parse --git-dir)/.env_secrets_hash"

[ -f "$ENV_FILE" ] || exit 0
command -v gh &>/dev/null || exit 0

current_hash=$(grep -E '^(BINANCE_TEST_|BYBIT_TEST_|TRADING_MODE=|EXCHANGE=)' "$ENV_FILE" | shasum -a 256 | cut -d' ' -f1)
previous_hash=""
[ -f "$HASH_FILE" ] && previous_hash=$(cat "$HASH_FILE")

if [ "$current_hash" != "$previous_hash" ]; then
    echo "⟳ .env test keys changed — syncing to GitHub secrets..."
    "$ROOT/scripts/run_session.sh" sync-secrets
    echo "$current_hash" > "$HASH_FILE"
fi
HOOK

chmod +x "$HOOK_DIR/pre-push"
echo "✓ pre-push hook installed"
