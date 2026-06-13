#!/usr/bin/env bash
# Called by GitHub Actions (or manually) to deploy latest main on the VM.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/pet-food-barcode-lookup}"
BRANCH="${BRANCH:-main}"
USE_NGINX="${USE_NGINX:-true}"

cd "$APP_DIR"

echo "==> Pulling latest $BRANCH..."
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull origin "$BRANCH"

preflight_github_ssh() {
  local remote_url
  remote_url="$(git remote get-url origin 2>/dev/null || true)"
  if [[ "$remote_url" != git@github.com:* && "$remote_url" != ssh://git@github.com/* ]]; then
    return 0
  fi
  # git ls-remote uses the same SSH credentials as pull; avoids ssh -T exit-code quirks.
  if git ls-remote --heads origin "$BRANCH" &>/dev/null; then
    echo "GitHub SSH OK"
    return 0
  fi
  echo ""
  echo "ERROR: GitHub SSH authentication failed on the VM."
  echo "The repo uses git@github.com but this user has no deploy key configured."
  echo "Fix: deploy/CICD.md Step 13.5 (or deploy/DEPLOY.md Step 9)."
  exit 1
}

preflight_github_ssh

COMPOSE_FILES=(-f docker-compose.yml)
if [[ "$USE_NGINX" == "true" ]]; then
  COMPOSE_FILES+=(-f docker-compose.prod.yml)
else
  COMPOSE_FILES+=(-f docker-compose.direct.yml)
  export APP_PORT="${APP_PORT:-80}"
fi

if [[ -f deploy/gcp-sa-key.json ]]; then
  COMPOSE_FILES+=(-f docker-compose.ec2.yml)
fi

echo "==> Stopping existing containers (frees ports when switching layouts)..."
docker compose "${COMPOSE_FILES[@]}" down --remove-orphans

echo "==> Rebuilding and restarting containers..."
docker compose "${COMPOSE_FILES[@]}" up -d --build --wait

echo "==> Pruning old images..."
docker image prune -f

echo "==> Health check..."
HEALTH_URL="http://127.0.0.1:8000/api/health"
MAX_ATTEMPTS=30
attempt=1
until curl -fsS "$HEALTH_URL" >/dev/null 2>&1; do
  if [[ "$attempt" -ge "$MAX_ATTEMPTS" ]]; then
    echo "ERROR: App did not respond on $HEALTH_URL after $((MAX_ATTEMPTS * 2))s"
    docker compose "${COMPOSE_FILES[@]}" ps
    docker compose "${COMPOSE_FILES[@]}" logs --tail=80 app
    exit 1
  fi
  echo "Waiting for app on :8000... ($attempt/$MAX_ATTEMPTS)"
  sleep 2
  attempt=$((attempt + 1))
done
curl -fsS "$HEALTH_URL"
echo ""
echo "Deploy OK at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
