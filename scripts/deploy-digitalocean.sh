#!/usr/bin/env bash
set -euo pipefail

# DigitalOcean deployment script
# Prerequisites: doctl CLI authenticated, SSH key added
#
# Usage:
#   ./scripts/deploy-digitalocean.sh [droplet-name]

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

# Copy project files
rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude 'logs' --exclude '.git' \
    ./ "root@${IP}:/opt/trading-bot/"

# Copy .env if it exists
if [ -f .env ]; then
    scp .env "root@${IP}:/opt/trading-bot/.env"
fi

# Build and run on the droplet
ssh "root@${IP}" << 'REMOTE'
cd /opt/trading-bot
docker compose down || true
docker compose up -d --build
docker compose logs -f --tail 50
REMOTE

echo "=== Deployment complete ==="
echo "SSH:  ssh root@${IP}"
echo "Logs: ssh root@${IP} 'cd /opt/trading-bot && docker compose logs -f'"
