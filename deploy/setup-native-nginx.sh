#!/usr/bin/env bash
# Put nginx in front of the native (non-Docker) app on port 8000.
# Usage: DOMAIN=api.mindmycat.com SSL_EMAIL=evans.manyala@gmail.com bash deploy/setup-native-nginx.sh

set -euo pipefail

DOMAIN="${DOMAIN:?Set DOMAIN, e.g. DOMAIN=api.mindmycat.com}"
APP_DIR="${APP_DIR:-$HOME/pet-food-barcode-lookup}"
EMAIL="${SSL_EMAIL:-}"

sudo apt-get update -qq
sudo apt-get install -y -qq nginx certbot python3-certbot-nginx

sudo cp "$APP_DIR/deploy/nginx/pet-food-lookup.conf.template" /tmp/pet-food-lookup.conf
sudo sed -i "s/DOMAIN/$DOMAIN/g" /tmp/pet-food-lookup.conf
sudo mv /tmp/pet-food-lookup.conf /etc/nginx/sites-available/pet-food-lookup
sudo ln -sf /etc/nginx/sites-available/pet-food-lookup /etc/nginx/sites-enabled/pet-food-lookup
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl reload nginx

sudo ufw allow 'Nginx Full' || { sudo ufw allow 80/tcp; sudo ufw allow 443/tcp; }

if [[ -n "$EMAIL" ]]; then
  sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --redirect
fi

echo "Done: https://$DOMAIN/  (app must be running on 127.0.0.1:8000)"
