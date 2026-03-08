#!/usr/bin/env bash
set -euo pipefail

# Build runtime secrets env files.
#
# Usage:
#   ./scripts/materialize_runtime_secrets.sh local
#   ./scripts/materialize_runtime_secrets.sh prod
#
# Output:
#   env/local.runtime.secrets.env or env/prod.runtime.secrets.env
#
# Behavior:
# - Prefer existing mode-specific secrets files (source of truth).
# - Fall back to legacy generation from .env BINANCE/BYBIT *_TEST/*_PROD keys.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-}"
if [[ "$MODE" != "local" && "$MODE" != "prod" ]]; then
  echo "Usage: $0 {local|prod}"
  exit 1
fi

if [[ "$MODE" == "local" ]]; then
  RUNTIME_ENV="$ROOT/env/local.runtime.env"
  OUTPUT_ENV="$ROOT/env/local.runtime.secrets.env"
  SRC_SUFFIX="TEST"
else
  RUNTIME_ENV="$ROOT/env/prod.runtime.env"
  OUTPUT_ENV="$ROOT/env/prod.runtime.secrets.env"
  SRC_SUFFIX="PROD"
fi

if [[ ! -f "$RUNTIME_ENV" && "$MODE" == "prod" && -f "$ROOT/env/prod.runtime.env.example" ]]; then
  RUNTIME_ENV="$ROOT/env/prod.runtime.env.example"
fi

if [[ ! -f "$RUNTIME_ENV" && "$MODE" == "local" ]]; then
  echo "ERROR: missing $RUNTIME_ENV"
  exit 1
fi

read_kv() {
  local file="$1"
  local key="$2"
  local value
  value="$(python3 - "$file" "$key" <<'PY'
import sys
from pathlib import Path
env_file = Path(sys.argv[1])
target = sys.argv[2]
value = ""
for line in env_file.read_text().splitlines():
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        continue
    k, v = s.split("=", 1)
    if k.strip() == target:
        value = v.strip()
        break
print(value)
PY
)"
  printf "%s" "$value"
}

read_runtime_exchange() {
  python3 - "$RUNTIME_ENV" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
exchange = ""
for line in p.read_text().splitlines():
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        continue
    k, v = s.split("=", 1)
    if k.strip() == "EXCHANGE":
        exchange = v.strip().lower()
        break
print(exchange)
PY
}

mask4() {
  local v="$1"
  if [[ -z "$v" ]]; then
    printf "MISSING"
  else
    printf "***%s" "${v: -4}"
  fi
}

resolve_deploy_commit() {
  local sha
  sha="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || true)"
  if [[ -z "$sha" ]]; then
    sha="unknown"
  fi
  printf "%s" "$sha"
}

stamp_deploy_commit() {
  local file="$1"
  local sha="$2"
  python3 - "$file" "$sha" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
sha = sys.argv[2]
lines = path.read_text().splitlines() if path.exists() else []
out = []
seen = False
for line in lines:
    s = line.strip()
    if s.startswith("DEPLOY_COMMIT="):
        out.append(f"DEPLOY_COMMIT={sha}")
        seen = True
    else:
        out.append(line)
if not seen:
    out.append(f"DEPLOY_COMMIT={sha}")
path.write_text("\n".join(out) + "\n")
PY
}

DEPLOY_COMMIT_SHA="$(resolve_deploy_commit)"

SOURCE_ENV="$ROOT/.env"
if [[ -f "$OUTPUT_ENV" ]]; then
  binance_key_existing="$(read_kv "$OUTPUT_ENV" "BINANCE_API_KEY")"
  binance_secret_existing="$(read_kv "$OUTPUT_ENV" "BINANCE_API_SECRET")"
  bybit_key_existing="$(read_kv "$OUTPUT_ENV" "BYBIT_API_KEY")"
  bybit_secret_existing="$(read_kv "$OUTPUT_ENV" "BYBIT_API_SECRET")"
  if [[ -n "$binance_key_existing" || -n "$binance_secret_existing" || -n "$bybit_key_existing" || -n "$bybit_secret_existing" ]]; then
    stamp_deploy_commit "$OUTPUT_ENV" "$DEPLOY_COMMIT_SHA"
    chmod 600 "$OUTPUT_ENV" || true
    echo "Using existing secrets file: $OUTPUT_ENV"
    echo "DEPLOY_COMMIT=$DEPLOY_COMMIT_SHA"
    echo "BINANCE_API_KEY=$(mask4 "$binance_key_existing")"
    echo "BINANCE_API_SECRET=$(mask4 "$binance_secret_existing")"
    echo "BYBIT_API_KEY=$(mask4 "$bybit_key_existing")"
    echo "BYBIT_API_SECRET=$(mask4 "$bybit_secret_existing")"
    exit 0
  fi
fi

if [[ ! -f "$SOURCE_ENV" ]]; then
  echo "ERROR: missing $SOURCE_ENV and no usable $OUTPUT_ENV"
  exit 1
fi

selected_exchange=""
if [[ -f "$RUNTIME_ENV" ]]; then
  selected_exchange="$(read_runtime_exchange)"
fi
if [[ -z "$selected_exchange" ]]; then
  if [[ "$MODE" == "prod" ]]; then
    selected_exchange="binance"
  else
    echo "ERROR: EXCHANGE is not set in $RUNTIME_ENV"
    exit 1
  fi
fi

selected_base="$selected_exchange"
if [[ "$selected_base" == binance* ]]; then
  selected_base="binance"
elif [[ "$selected_base" == bybit* ]]; then
  selected_base="bybit"
fi

binance_key="$(read_kv "$SOURCE_ENV" "BINANCE_${SRC_SUFFIX}_API_KEY")"
binance_secret="$(read_kv "$SOURCE_ENV" "BINANCE_${SRC_SUFFIX}_API_SECRET")"
bybit_key="$(read_kv "$SOURCE_ENV" "BYBIT_${SRC_SUFFIX}_API_KEY")"
bybit_secret="$(read_kv "$SOURCE_ENV" "BYBIT_${SRC_SUFFIX}_API_SECRET")"

if [[ "$selected_base" == "binance" ]]; then
  if [[ -z "$binance_key" || -z "$binance_secret" ]]; then
    echo "ERROR: Missing BINANCE_${SRC_SUFFIX}_API_KEY/SECRET in $SOURCE_ENV for mode=$MODE"
    exit 1
  fi
elif [[ "$selected_base" == "bybit" ]]; then
  if [[ -z "$bybit_key" || -z "$bybit_secret" ]]; then
    echo "ERROR: Missing BYBIT_${SRC_SUFFIX}_API_KEY/SECRET in $SOURCE_ENV for mode=$MODE"
    exit 1
  fi
fi

{
  echo "# Auto-generated by scripts/materialize_runtime_secrets.sh ($MODE)"
  echo "# Do not commit. Source: .env (${SRC_SUFFIX} keys)"
  echo "BINANCE_API_KEY=$binance_key"
  echo "BINANCE_API_SECRET=$binance_secret"
  echo "BYBIT_API_KEY=$bybit_key"
  echo "BYBIT_API_SECRET=$bybit_secret"
} > "$OUTPUT_ENV"

stamp_deploy_commit "$OUTPUT_ENV" "$DEPLOY_COMMIT_SHA"

chmod 600 "$OUTPUT_ENV" || true

echo "Generated: $OUTPUT_ENV"
echo "DEPLOY_COMMIT=$DEPLOY_COMMIT_SHA"
echo "EXCHANGE(base): $selected_base"
echo "BINANCE_API_KEY=$(mask4 "$binance_key")"
echo "BINANCE_API_SECRET=$(mask4 "$binance_secret")"
echo "BYBIT_API_KEY=$(mask4 "$bybit_key")"
echo "BYBIT_API_SECRET=$(mask4 "$bybit_secret")"
