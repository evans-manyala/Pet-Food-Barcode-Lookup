#!/usr/bin/env bash
# Bootstrap Ubuntu 22.04+ VM (GCP or EC2) for Pet Food Barcode Lookup.
# Run as a user with sudo: bash deploy/setup-vm.sh

set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/pet-food-barcode-lookup}"
PLATFORM="${PLATFORM:-gcp}"   # gcp | ec2
APP_PORT="${APP_PORT:-80}"

echo "==> Installing Docker..."
sudo apt-get update -qq
sudo apt-get install -y -qq ca-certificates curl git ufw
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "${VERSION_CODENAME:-$VERSION_ID}") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update -qq
sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER" || true

echo "==> Configuring firewall (allow SSH + HTTP)..."
sudo ufw allow OpenSSH
sudo ufw allow "${APP_PORT}/tcp"
sudo ufw --force enable

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "==> Creating .env from template..."
  cp "$APP_DIR/deploy/env.production.example" "$APP_DIR/.env"
  echo "    Edit $APP_DIR/.env with your API keys before starting."
fi

cd "$APP_DIR"

COMPOSE_FILES=(-f docker-compose.yml)
if [[ "$PLATFORM" == "ec2" ]]; then
  if [[ ! -f "${GCP_SA_KEY_PATH:-$APP_DIR/deploy/gcp-sa-key.json}" ]]; then
    echo "ERROR: EC2 requires a GCP service account key at deploy/gcp-sa-key.json"
    echo "       Create one in GCP Console → IAM → Service Accounts → Keys"
    exit 1
  fi
  COMPOSE_FILES+=(-f docker-compose.ec2.yml)
fi

echo "==> Building and starting services (platform=$PLATFORM, port=$APP_PORT)..."
export APP_PORT
sudo docker compose "${COMPOSE_FILES[@]}" up -d --build

PUBLIC_IP=""
if command -v curl >/dev/null 2>&1; then
  PUBLIC_IP=$(curl -fsS -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || true)
  if [[ -z "$PUBLIC_IP" ]]; then
    PUBLIC_IP=$(curl -fsS http://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)
  fi
fi

echo ""
echo "=============================================="
echo " Deployment complete"
echo "=============================================="
if [[ -n "$PUBLIC_IP" ]]; then
  echo " Share this URL with testers:"
  echo "   http://${PUBLIC_IP}:${APP_PORT}/"
  echo "   http://${PUBLIC_IP}:${APP_PORT}/?barcode=9003579008331"
else
  echo " Open http://<your-vm-public-ip>:${APP_PORT}/ in a browser"
fi
echo ""
echo " Useful commands:"
echo "   sudo docker compose logs -f app"
echo "   sudo docker compose restart app"
echo "   sudo docker compose down"
echo ""
echo " Re-login may be required for docker group: newgrp docker"
