#!/usr/bin/env bash
# Configure nginx + Let's Encrypt SSL for api.yourdomain.com
# Run on the VM after DNS A-record points to this server's public IP.
#
# Usage:
#   DOMAIN=api.mydomain.com APP_DIR=$HOME/pet-food-barcode-lookup bash deploy/setup-domain.sh

set -euo pipefail

DOMAIN="${DOMAIN:?Set DOMAIN, e.g. DOMAIN=api.mydomain.com}"
APP_DIR="${APP_DIR:-$HOME/pet-food-barcode-lookup}"
EMAIL="${SSL_EMAIL:-}"

echo "==> Installing nginx and certbot..."
sudo apt-get update -qq
sudo apt-get install -y -qq nginx certbot python3-certbot-nginx

echo "==> Writing nginx site config for $DOMAIN..."
sudo cp "$APP_DIR/deploy/nginx/pet-food-lookup.conf.template" /tmp/pet-food-lookup.conf
sudo sed -i "s/DOMAIN/$DOMAIN/g" /tmp/pet-food-lookup.conf
sudo mv /tmp/pet-food-lookup.conf /etc/nginx/sites-available/pet-food-lookup
sudo ln -sf /etc/nginx/sites-available/pet-food-lookup /etc/nginx/sites-enabled/pet-food-lookup
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl reload nginx

echo "==> Opening firewall for HTTP/HTTPS..."
sudo ufw allow 'Nginx Full' || sudo ufw allow 80/tcp && sudo ufw allow 443/tcp

echo "==> Starting app on localhost:8000 (behind nginx)..."
cd "$APP_DIR"
export APP_PORT=8000
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.prod.yml)
if [[ -f deploy/gcp-sa-key.json ]]; then
  COMPOSE_FILES+=(-f docker-compose.ec2.yml)
fi
sudo docker compose "${COMPOSE_FILES[@]}" up -d --build

if [[ -z "$EMAIL" ]]; then
  echo ""
  echo "DNS check: ensure $DOMAIN has an A record pointing to this VM's public IP."
  echo "Then run:"
  echo "  sudo certbot --nginx -d $DOMAIN"
  echo ""
  echo "Or re-run with: SSL_EMAIL=you@mydomain.com bash deploy/setup-domain.sh"
else
  echo "==> Requesting TLS certificate..."
  sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --redirect
fi

echo ""
echo "=============================================="
echo " Domain setup complete"
echo "=============================================="
echo "  https://$DOMAIN/"
echo "  https://$DOMAIN/api/health"
