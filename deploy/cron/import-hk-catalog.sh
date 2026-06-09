#!/usr/bin/env bash
# Re-import scraped HK retailer CSVs into the local SQLite catalog.
#
# Default CSV locations (no machine-specific paths required):
#   data/imports/hktvmall/
#   data/imports/shopify/
#
# Example crontab (weekly Sunday 03:00 UTC):
#   0 3 * * 0 /path/to/Pet-Food-Barcode-Lookup/deploy/cron/import-hk-catalog.sh >> /var/log/pet-food-catalog-import.log 2>&1

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting HK catalog import …"
echo "Project root: $ROOT"
python "$ROOT/scripts/import_hk_catalog.py" "$@"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] HK catalog import finished."
