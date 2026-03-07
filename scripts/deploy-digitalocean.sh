#!/usr/bin/env bash
set -euo pipefail

# DigitalOcean deployment script
# Prerequisites: doctl CLI authenticated, SSH key added
#
# Usage:
#   ./scripts/deploy-digitalocean.sh [droplet-name]
#
# Deploy semantics:
# - Never copy local .env to prod (local file may contain test/local secrets).
# - Prefer env/prod.compose.env + env/prod.runtime.env from this machine, if present.
# - Otherwise keep remote prod env files and validate they exist before compose.

DROPLET_NAME="${1:-trading-bot}"
REGION="nyc1"
SIZE="s-1vcpu-1gb"     # $6/month - enough for a single bot
IMAGE="docker-20-04"

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

# Build and run on the droplet
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

RUNTIME_ENV_OVERRIDE_FILE=env/prod.runtime.env \
RUNTIME_SECRETS_FILE=env/prod.runtime.secrets.env \
docker compose --env-file .env --env-file env/prod.compose.env down || true
RUNTIME_ENV_OVERRIDE_FILE=env/prod.runtime.env \
RUNTIME_SECRETS_FILE=env/prod.runtime.secrets.env \
docker compose --env-file .env --env-file env/prod.compose.env up -d --build
RUNTIME_ENV_OVERRIDE_FILE=env/prod.runtime.env \
RUNTIME_SECRETS_FILE=env/prod.runtime.secrets.env \
docker compose --env-file .env --env-file env/prod.compose.env logs -f --tail 50
REMOTE

echo "=== Deployment complete ==="
echo "SSH:  ssh root@${IP}"
echo "Logs: ssh root@${IP} 'cd /opt/trading-bot && docker compose logs -f'"
