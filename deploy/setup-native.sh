#!/usr/bin/env bash
# Native deploy (no Docker) — Python venv + Redis + systemd on Ubuntu 22.04+.
#
# Usage (on the VM, after cloning repo and creating .env):
#   bash deploy/setup-native.sh
#
# Optional — expose on port 80 via nginx:
#   DOMAIN=api.mindmycat.com SSL_EMAIL=evans.manyala@gmail.com bash deploy/setup-domain.sh
#   (setup-domain.sh detects no Docker and skips container steps if you use setup-native-nginx.sh instead)

set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/pet-food-barcode-lookup}"
PORT="${APP_PORT:-8000}"

echo "==> Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip redis-server git curl

echo "==> Enabling Redis..."
sudo systemctl enable redis-server
sudo systemctl start redis-server
redis-cli ping

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "ERROR: $APP_DIR/.env not found. Copy deploy/env.production.example to .env first."
  exit 1
fi

echo "==> Creating Python virtualenv..."
cd "$APP_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Installing systemd service..."
# Replace %h/%i placeholders with actual home dir and username
USER_NAME="$(whoami)"
HOME_DIR="$HOME"
sed "s|%h|$HOME_DIR|g; s|%i|$USER_NAME|g" \
  "$APP_DIR/deploy/systemd/pet-food-lookup.service" \
  | sudo tee /etc/systemd/system/pet-food-lookup.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable pet-food-lookup
sudo systemctl restart pet-food-lookup

echo "==> Opening firewall for port $PORT..."
sudo ufw allow OpenSSH
sudo ufw allow "${PORT}/tcp"
sudo ufw --force enable || true

sleep 2
if curl -fsS "http://127.0.0.1:${PORT}/api/health" > /dev/null; then
  echo ""
  echo "=============================================="
  echo " Native deploy OK"
  echo "=============================================="
  echo "  http://$(curl -fsS -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || echo '<VM_IP>'):${PORT}/"
  echo ""
  echo " Commands:"
  echo "   sudo systemctl status pet-food-lookup"
  echo "   sudo journalctl -u pet-food-lookup -f      # live logs"
  echo "   sudo systemctl restart pet-food-lookup     # after .env changes"
else
  echo "WARN: health check failed — check logs:"
  echo "  sudo journalctl -u pet-food-lookup -n 50 --no-pager"
  exit 1
fi
