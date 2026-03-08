#!/usr/bin/env bash
set -euo pipefail

# DigitalOcean deployment script
# Prerequisites: doctl CLI authenticated, SSH key added
#
# Usage:
#   ./scripts/deploy-digitalocean.sh [droplet-name]
#
# Deploy semantics:
# - Build prod images locally.
# - Upload image bundle to DO, load there, and run without remote build.
# - Keep only the latest 2 local image bundles.
# - Never copy local .env to prod.

DROPLET_NAME="${1:-trading-bot}"
REGION="nyc1"
SIZE="s-1vcpu-1gb"     # $6/month - enough for a single bot
IMAGE="docker-20-04"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ARTIFACT_ROOT="$ROOT_DIR/.artifacts/prod-image-sets"
KEEP_IMAGE_SETS="${KEEP_IMAGE_SETS:-2}"
SET_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD)"
SET_DIR="$ARTIFACT_ROOT/$SET_ID"
SET_ARCHIVE="$SET_DIR/images.tar.gz"

mkdir -p "$SET_DIR"

if [ ! -f .env ]; then
    echo "ERROR: missing .env in repo root"
    exit 1
fi
if [ ! -f env/prod.compose.env ]; then
    echo "ERROR: missing env/prod.compose.env"
    exit 1
fi
if [ ! -f env/prod.runtime.env ]; then
    echo "ERROR: missing env/prod.runtime.env"
    exit 1
fi

echo "=== Trading Bot - DigitalOcean Deployment ==="

# Check if droplet exists
if doctl compute droplet list --format Name --no-header | grep -q "^${DROPLET_NAME}$"; then
    echo "Droplet '${DROPLET_NAME}' already exists."
    IP=$(doctl compute droplet get "${DROPLET_NAME}" --format PublicIPv4 --no-header)
else
    echo "Creating droplet '${DROPLET_NAME}'..."
    doctl compute droplet create "${DROPLET_NAME}" \
        --region "${REGION}" \
        --size "${SIZE}" \
        --image "${IMAGE}" \
        --ssh-keys "$(doctl compute ssh-key list --format ID --no-header | head -1)" \
        --wait

    IP=$(doctl compute droplet get "${DROPLET_NAME}" --format PublicIPv4 --no-header)
    echo "Droplet created at ${IP}"
    echo "Waiting for SSH to become available..."
    sleep 30
fi

echo "Deploying to ${IP}..."

# Build prod runtime secrets from local .env before transfer.
./scripts/materialize_runtime_secrets.sh prod
if [ ! -f env/prod.runtime.secrets.env ]; then
    echo "ERROR: missing env/prod.runtime.secrets.env after materialization"
    exit 1
fi

dc_prod_local() {
    RUNTIME_ENV_OVERRIDE_FILE=env/prod.runtime.env \
    RUNTIME_SECRETS_FILE=env/prod.runtime.secrets.env \
    docker compose --env-file .env --env-file env/prod.compose.env "$@"
}

echo "Building prod images locally..."
dc_prod_local build

IMAGES=(
    "trading-bot-bot-hub"
    "trading-bot-openclaw-bridge"
    "trading-bot-bot-momentum"
    "trading-bot-bot-indicators"
    "trading-bot-bot-meanrev"
    "trading-bot-bot-swing"
    "trading-bot-bot-extreme"
    "trading-bot-bot-scalper"
    "trading-bot-bot-fullstack"
    "trading-bot-bot-conservative"
    "trading-bot-bot-aggressive"
    "trading-bot-bot-hedger"
    "trading-bot-loki"
    "trading-bot-promtail"
    "trading-bot-prometheus"
    "trading-bot-grafana"
)

for img in "${IMAGES[@]}"; do
    if ! docker image inspect "$img" >/dev/null 2>&1; then
        echo "ERROR: expected local image missing: $img"
        exit 1
    fi
done

echo "Packing local image set: $SET_ID"
printf '%s\n' "${IMAGES[@]}" > "$SET_DIR/images.txt"
docker save "${IMAGES[@]}" | gzip > "$SET_ARCHIVE"
echo "$SET_ID" > "$SET_DIR/set_id.txt"

# Rolling retention for local image bundles.
if [ "$KEEP_IMAGE_SETS" -gt 0 ]; then
    mapfile -t _sets < <(ls -1dt "$ARTIFACT_ROOT"/* 2>/dev/null || true)
    if [ "${#_sets[@]}" -gt "$KEEP_IMAGE_SETS" ]; then
        for old_set in "${_sets[@]:$KEEP_IMAGE_SETS}"; do
            rm -rf "$old_set"
        done
    fi
fi

# Copy project files
rsync -avz \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude 'logs' \
    --exclude '.git' \
    --exclude '.env' \
    --exclude 'env/local.compose.env' \
    --exclude 'env/local.runtime.env' \
    --exclude 'env/local.runtime.secrets.env' \
    --exclude 'env/prod.runtime.secrets.env' \
    --exclude '.artifacts' \
    ./ "root@${IP}:/opt/trading-bot/"

# Optionally copy prod env overrides from local machine when available.
if [ -f env/prod.compose.env ]; then
    scp env/prod.compose.env "root@${IP}:/opt/trading-bot/env/prod.compose.env"
    echo "Copied env/prod.compose.env"
fi
if [ -f env/prod.runtime.env ]; then
    scp env/prod.runtime.env "root@${IP}:/opt/trading-bot/env/prod.runtime.env"
    echo "Copied env/prod.runtime.env"
fi
if [ -f env/prod.runtime.secrets.env ]; then
    scp env/prod.runtime.secrets.env "root@${IP}:/opt/trading-bot/env/prod.runtime.secrets.env"
    echo "Copied env/prod.runtime.secrets.env"
fi

ssh "root@${IP}" "mkdir -p /opt/trading-bot/.artifacts/prod-image-sets/$SET_ID"
scp "$SET_ARCHIVE" "root@${IP}:/opt/trading-bot/.artifacts/prod-image-sets/$SET_ID/images.tar.gz"
scp "$SET_DIR/images.txt" "root@${IP}:/opt/trading-bot/.artifacts/prod-image-sets/$SET_ID/images.txt"
scp "$SET_DIR/set_id.txt" "root@${IP}:/opt/trading-bot/.artifacts/prod-image-sets/$SET_ID/set_id.txt"
echo "Uploaded image bundle: $SET_ID"

# Load and run on the droplet (no remote build)
ssh "root@${IP}" << 'REMOTE'
cd /opt/trading-bot

if [ ! -f .env ]; then
  echo "ERROR: missing /opt/trading-bot/.env on target host"; exit 1
fi
if [ ! -f env/prod.compose.env ]; then
  echo "ERROR: missing /opt/trading-bot/env/prod.compose.env on target host"; exit 1
fi
if [ ! -f env/prod.runtime.env ]; then
  echo "ERROR: missing /opt/trading-bot/env/prod.runtime.env on target host"; exit 1
fi
if [ ! -f env/prod.runtime.secrets.env ]; then
  echo "ERROR: missing /opt/trading-bot/env/prod.runtime.secrets.env on target host"; exit 1
fi
if [ ! -d .artifacts/prod-image-sets ]; then
  echo "ERROR: missing uploaded image sets directory"; exit 1
fi

latest_set="$(ls -1dt .artifacts/prod-image-sets/* 2>/dev/null | head -n1 || true)"
if [ -z "$latest_set" ] || [ ! -f "$latest_set/images.tar.gz" ]; then
  echo "ERROR: missing uploaded image bundle"; exit 1
fi

echo "Loading image bundle: $latest_set"
gunzip -c "$latest_set/images.tar.gz" | docker load

dc_prod() {
  RUNTIME_ENV_OVERRIDE_FILE=env/prod.runtime.env \
  RUNTIME_SECRETS_FILE=env/prod.runtime.secrets.env \
  docker compose --env-file .env --env-file env/prod.compose.env "$@"
}

# Guardrail: fail fast if prod runtime files are not wired into compose config.
dc_cfg="$(dc_prod config 2>&1)"
if printf "%s\n" "$dc_cfg" | rg -q 'level=warning msg="The \"RUNTIME_(ENV_OVERRIDE_FILE|SECRETS_FILE)\" variable is not set'; then
  echo "ERROR: prod compose config still reports missing runtime vars"; exit 1
fi
if ! printf "%s\n" "$dc_cfg" | rg -q 'path: env/prod.runtime.env'; then
  echo "ERROR: prod runtime override file not resolved in compose config"; exit 1
fi
if ! printf "%s\n" "$dc_cfg" | rg -q 'path: env/prod.runtime.secrets.env'; then
  echo "ERROR: prod runtime secrets file not resolved in compose config"; exit 1
fi

# Keep only latest 2 uploaded bundles on remote.
mapfile -t _remote_sets < <(ls -1dt .artifacts/prod-image-sets/* 2>/dev/null || true)
if [ "${#_remote_sets[@]}" -gt 2 ]; then
  for old_set in "${_remote_sets[@]:2}"; do
    rm -rf "$old_set"
  done
fi

dc_prod down || true
dc_prod up -d --no-build
dc_prod logs -f --tail 50
REMOTE

echo "=== Deployment complete ==="
echo "SSH:  ssh root@${IP}"
echo "Logs: ssh root@${IP} 'cd /opt/trading-bot && docker compose logs -f'"
