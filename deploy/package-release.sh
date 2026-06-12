#!/usr/bin/env bash
# Create a deployable tarball (excludes secrets and local venv).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/release.tar.gz"

tar -czf "$OUT" \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='.env' \
  --exclude='deploy/gcp-sa-key.json' \
  --exclude='__pycache__' \
  --exclude='release.tar.gz' \
  -C "$ROOT" \
  api src frontend main.py requirements.txt Dockerfile docker-compose.yml docker-compose.direct.yml docker-compose.prod.yml docker-compose.ec2.yml deploy

echo "Created $OUT"
echo "Upload to VM: scp release.tar.gz user@VM_IP:~/ && ssh user@VM_IP 'tar -xzf release.tar.gz'"
