#!/usr/bin/env bash
set -euo pipefail

# DigitalOcean deployment script
# Prerequisites:
# - Preferred: doctl CLI authenticated (for droplet discovery/provisioning)
# - Always: SSH key available for target host access
#
# Usage:
#   ./scripts/deploy-digitalocean.sh [droplet-name]
#
# Deploy semantics:
# - Build prod images locally.
# - Export/upload/load each image separately (no monolithic archive).
# - Keep only the latest 2 local image sets.
# - Never copy local .env to prod.

DROPLET_NAME="${1:-trading-bot}"
REGION="nyc1"
SIZE="s-1vcpu-1gb"     # $6/month - enough for a single bot
IMAGE="docker-20-04"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Prevent overlapping deploy runs (single-instance lock, portable).
LOCK_DIR="/tmp/trading-bot-prod-deploy.lockdir"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "ERROR: deploy already running (lock: $LOCK_DIR)"
    exit 1
fi
cleanup_lock() {
    rmdir "$LOCK_DIR" >/dev/null 2>&1 || true
}
trap cleanup_lock EXIT INT TERM

# Docker CLI is not always on PATH in sandboxed shells.
if ! command -v docker >/dev/null 2>&1; then
    export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
fi

# Load deploy-specific overrides (host/user/key/platform) from prod compose env.
set -a
# shellcheck disable=SC1091
. "$ROOT_DIR/env/prod.compose.env"
set +a

TARGET_HOST="${DEPLOY_DROPLET_HOST:-}"
SSH_USER="${DEPLOY_SSH_USER:-root}"
SSH_KEY_PATH="${DEPLOY_SSH_KEY_PATH:-}"
DEPLOY_IMAGE_PLATFORM="${DEPLOY_IMAGE_PLATFORM:-linux/amd64}"
SSH_OPTS=()
if [ -n "$SSH_KEY_PATH" ]; then
    SSH_OPTS=(
        -i "$SSH_KEY_PATH"
        -o IdentitiesOnly=yes
        -o StrictHostKeyChecking=accept-new
        -o BatchMode=yes
        -o ConnectTimeout=10
        -o ServerAliveInterval=10
        -o ServerAliveCountMax=3
    )
fi

ARTIFACT_ROOT="$ROOT_DIR/.artifacts/prod-image-sets"
KEEP_IMAGE_SETS="${KEEP_IMAGE_SETS:-2}"
SET_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD)"
SET_DIR="$ARTIFACT_ROOT/$SET_ID"
MIN_LOCAL_KB="${DEPLOY_MIN_LOCAL_FREE_KB:-20971520}"   # 20 GiB
MIN_REMOTE_KB="${DEPLOY_MIN_REMOTE_FREE_KB:-10485760}" # 10 GiB

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
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker CLI is not available"
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "ERROR: docker daemon is not reachable"
    exit 1
fi
local_free_kb="$(df -Pk "$ROOT_DIR" | awk 'NR==2 {print $4}')"
if [ -z "$local_free_kb" ] || [ "$local_free_kb" -lt "$MIN_LOCAL_KB" ]; then
    echo "ERROR: not enough local free disk space (need >= $MIN_LOCAL_KB KB)"
    exit 1
fi

echo "=== Trading Bot - DigitalOcean Deployment ==="

if doctl account get >/dev/null 2>&1; then
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
else
    if [ -z "$TARGET_HOST" ]; then
        echo "ERROR: doctl auth unavailable and DEPLOY_DROPLET_HOST is not set"
        exit 1
    fi
    IP="$TARGET_HOST"
    echo "doctl auth unavailable; using configured host ${IP}"
fi

echo "Deploying to ${IP}..."
remote_free_kb="$(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${IP}" "df -Pk /opt | awk 'NR==2 {print \$4}'")"
if [ -z "$remote_free_kb" ] || [ "$remote_free_kb" -lt "$MIN_REMOTE_KB" ]; then
    echo "ERROR: not enough remote free disk space on /opt (need >= $MIN_REMOTE_KB KB)"
    exit 1
fi

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
DOCKER_DEFAULT_PLATFORM="$DEPLOY_IMAGE_PLATFORM" dc_prod_local build \
    bot-hub openclaw-bridge \
    bot-extreme bot-hedger bot-indicators bot-swing \
    loki promtail prometheus grafana

IMAGES=(
    "trading-bot-bot-hub"
    "trading-bot-openclaw-bridge"
    "trading-bot-bot-indicators"
    "trading-bot-bot-swing"
    "trading-bot-bot-extreme"
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

echo "Packing local image set per-image: $SET_ID"
printf '%s\n' "${IMAGES[@]}" > "$SET_DIR/images.txt"
for img in "${IMAGES[@]}"; do
    img_archive="$SET_DIR/${img}.tar.gz"
    echo " - exporting $img"
    docker save "$img" | gzip > "$img_archive"
done
echo "$SET_ID" > "$SET_DIR/set_id.txt"

# Rolling retention for local image bundles.
if [ "$KEEP_IMAGE_SETS" -gt 0 ]; then
    ls -1dt "$ARTIFACT_ROOT"/* 2>/dev/null | awk -v keep="$KEEP_IMAGE_SETS" 'NR>keep' | while IFS= read -r old_set; do
        [ -n "$old_set" ] && rm -rf "$old_set"
    done
fi

# Copy deployment manifest only (runtime code comes from uploaded images).
scp "${SSH_OPTS[@]}" docker-compose.yml "${SSH_USER}@${IP}:/opt/trading-bot/docker-compose.yml"

# Optionally copy prod env overrides from local machine when available.
if [ -f env/prod.compose.env ]; then
    scp "${SSH_OPTS[@]}" env/prod.compose.env "${SSH_USER}@${IP}:/opt/trading-bot/env/prod.compose.env"
    echo "Copied env/prod.compose.env"
fi
if [ -f env/prod.runtime.env ]; then
    scp "${SSH_OPTS[@]}" env/prod.runtime.env "${SSH_USER}@${IP}:/opt/trading-bot/env/prod.runtime.env"
    echo "Copied env/prod.runtime.env"
fi

ssh "${SSH_OPTS[@]}" "${SSH_USER}@${IP}" "mkdir -p /opt/trading-bot/.artifacts/prod-image-sets/$SET_ID"
for img in "${IMAGES[@]}"; do
    scp "${SSH_OPTS[@]}" "$SET_DIR/${img}.tar.gz" "${SSH_USER}@${IP}:/opt/trading-bot/.artifacts/prod-image-sets/$SET_ID/${img}.tar.gz"
done
scp "${SSH_OPTS[@]}" "$SET_DIR/images.txt" "${SSH_USER}@${IP}:/opt/trading-bot/.artifacts/prod-image-sets/$SET_ID/images.txt"
scp "${SSH_OPTS[@]}" "$SET_DIR/set_id.txt" "${SSH_USER}@${IP}:/opt/trading-bot/.artifacts/prod-image-sets/$SET_ID/set_id.txt"
echo "Uploaded image set: $SET_ID"

# Load and run on the droplet (no remote build)
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${IP}" << 'REMOTE'
cd /opt/trading-bot

if [ ! -f .env ]; then
  printf '# minimal compose env anchor\n' > .env
fi
if [ ! -f env/prod.compose.env ]; then
  echo "ERROR: missing /opt/trading-bot/env/prod.compose.env on target host"; exit 1
fi
if [ ! -f env/prod.runtime.env ]; then
  echo "ERROR: missing /opt/trading-bot/env/prod.runtime.env on target host"; exit 1
fi
if [ ! -d .artifacts/prod-image-sets ]; then
  echo "ERROR: missing uploaded image sets directory"; exit 1
fi

latest_set="$(ls -1dt .artifacts/prod-image-sets/* 2>/dev/null | head -n1 || true)"
if [ -z "$latest_set" ] || [ ! -f "$latest_set/images.txt" ]; then
  echo "ERROR: missing uploaded image set metadata"; exit 1
fi

# Backup currently loaded trading-bot images before replacement.
backup_root=".artifacts/pre-deploy-image-backups"
backup_id="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="$backup_root/$backup_id"
mkdir -p "$backup_dir"
docker images --format '{{.Repository}}' | grep '^trading-bot-' | sort -u > "$backup_dir/images.txt" || true
while IFS= read -r img; do
  [ -z "$img" ] && continue
  docker save "$img" | gzip > "$backup_dir/${img}.tar.gz"
done < "$backup_dir/images.txt"

echo "Loading images from set: $latest_set"
while IFS= read -r img; do
  [ -z "$img" ] && continue
  img_archive="$latest_set/${img}.tar.gz"
  if [ ! -f "$img_archive" ]; then
    echo "ERROR: missing image archive: $img_archive"; exit 1
  fi
  echo " - loading $img"
  gunzip -c "$img_archive" | docker load
done < "$latest_set/images.txt"

dc_prod() {
  RUNTIME_ENV_OVERRIDE_FILE=env/prod.runtime.env \
  RUNTIME_SECRETS_FILE=env/prod.runtime.secrets.env \
  docker compose --env-file .env --env-file env/prod.compose.env "$@"
}

# Guardrail: fail fast if prod runtime files are not wired into compose config.
dc_cfg="$(dc_prod config 2>&1)"
if printf "%s\n" "$dc_cfg" | grep -Eq 'level=warning msg="The "RUNTIME_ENV_OVERRIDE_FILE" variable is not set'; then
  echo "ERROR: prod compose config still reports missing runtime env override var"; exit 1
fi
if ! printf "%s\n" "$dc_cfg" | grep -q 'path: env/prod.runtime.env'; then
  echo "ERROR: prod runtime override file not resolved in compose config"; exit 1
fi

# Keep only latest 2 uploaded bundles on remote.
ls -1dt .artifacts/prod-image-sets/* 2>/dev/null | awk 'NR>2' | while IFS= read -r old_set; do
  [ -n "$old_set" ] && rm -rf "$old_set"
done

dc_prod down || true
dc_prod up -d --no-build \
  bot-hub-postgres bot-hub openclaw-bridge \
  bot-extreme bot-hedger bot-indicators bot-swing \
  loki promtail prometheus grafana
# Ensure non-deployed trade bot containers are absent on prod host.
for old_bot in bot-momentum bot-meanrev bot-scalper bot-fullstack bot-conservative bot-aggressive; do
  docker rm -f "$old_bot" >/dev/null 2>&1 || true
done
# Enforce no auto-start on droplet reboot for all deployed services.
docker ps -aq | xargs -r docker update --restart=no >/dev/null
REMOTE

echo "=== Deployment complete ==="
echo "SSH:  ssh ${SSH_USER}@${IP}"
echo "Logs: ssh ${SSH_USER}@${IP} 'cd /opt/trading-bot && docker compose logs -f'"
rm -f env/prod.runtime.secrets.env
echo "Cleaning local image copies used for deploy..."
docker image rm "${IMAGES[@]}" >/dev/null 2>&1 || true
