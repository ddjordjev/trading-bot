#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "[ERROR] Missing virtualenv python at $PY"
    exit 1
fi

OPENCLAW_URL="${OPENCLAW_URL:-http://localhost:18080/intel}"
OPENCLAW_TOKEN="${OPENCLAW_TOKEN:-}"
HUB_URL="${HUB_URL:-http://localhost:9035}"
DASHBOARD_TOKEN="${DASHBOARD_TOKEN:-}"

echo "[OpenClaw] Endpoint validation"
"$PY" scripts/validate_openclaw_endpoint.py --url "$OPENCLAW_URL" --token "$OPENCLAW_TOKEN"

echo "[OpenClaw] Hub surface validation"
"$PY" scripts/validate_openclaw_hub_integration.py --hub-url "$HUB_URL" --dashboard-token "$DASHBOARD_TOKEN"

echo "[OK] OpenClaw preflight passed."
