#!/usr/bin/env bash
# Called by GitHub Actions (or manually) to deploy latest main on the VM.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/pet-food-barcode-lookup}"
BRANCH="${BRANCH:-main}"
USE_NGINX="${USE_NGINX:-true}"

cd "$APP_DIR"

echo "==> Pulling latest $BRANCH..."
if ! git fetch origin "$BRANCH"; then
  ORIGIN_URL="$(git remote get-url origin)"
  HTTPS_URL=""

  if [[ "$ORIGIN_URL" =~ ^git@github\.com:(.+)$ ]]; then
    HTTPS_URL="https://github.com/${BASH_REMATCH[1]}"
  elif [[ "$ORIGIN_URL" =~ ^ssh://git@github\.com/(.+)$ ]]; then
    HTTPS_URL="https://github.com/${BASH_REMATCH[1]}"
  fi

  if [[ -z "$HTTPS_URL" ]]; then
    echo "ERROR: git fetch failed and origin URL is not a supported GitHub SSH URL: $ORIGIN_URL"
    exit 1
  fi

  echo "==> SSH fetch failed; switching origin to HTTPS and retrying..."
  git remote set-url origin "$HTTPS_URL"
  git fetch origin "$BRANCH"
fi
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

COMPOSE_FILES=(-f docker-compose.yml)
if [[ "$USE_NGINX" == "true" ]]; then
  COMPOSE_FILES+=(-f docker-compose.prod.yml)
  export APP_PORT=8000
else
  export APP_PORT="${APP_PORT:-80}"
fi

if [[ -f deploy/gcp-sa-key.json ]]; then
  COMPOSE_FILES+=(-f docker-compose.ec2.yml)
fi

echo "==> Rebuilding and restarting containers..."
docker compose "${COMPOSE_FILES[@]}" up -d --build

echo "==> Pruning old images..."
docker image prune -f

echo "==> Health check..."
sleep 3
curl -fsS http://127.0.0.1:8000/api/health || curl -fsS "http://127.0.0.1:${APP_PORT:-80}/api/health"
echo ""
echo "Deploy OK at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
