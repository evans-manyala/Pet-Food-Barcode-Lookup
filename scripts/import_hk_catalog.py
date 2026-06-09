#!/usr/bin/env python3
"""
Import scraped HK retailer CSV files into the local SQLite catalog.

Default CSV locations (inside the repo, portable on any machine):
  data/imports/hktvmall/   — HKTVmall worksheet CSV files
  data/imports/shopify/    — Vetopia, WnP, Pettington, QPets CSV files

Example:
  # Uses paths from .env or the defaults above
  python scripts/import_hk_catalog.py

  # One-off override
  python scripts/import_hk_catalog.py \\
    --hktvmall-dir /other/machine/path/hktvmall_worksheets_csv_files \\
    --shopify-dir /other/machine/path/converted_worksheets_csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.catalog.importers import import_hktvmall_dir, import_shopify_dir
from src.catalog.paths import (
    DEFAULT_HKTVMALL_IMPORT_DIR,
    DEFAULT_SHOPIFY_IMPORT_DIR,
    PROJECT_ROOT as CATALOG_ROOT,
    resolve_import_dir,
)
from src.catalog.store import HKCatalogStore
from src.config import get_settings

log = logging.getLogger(__name__)


def _resolve_arg_or_config(cli_path: Path | None, config_value: str, default_relative: str) -> Path:
    if cli_path is not None:
        return resolve_import_dir(str(cli_path), default_relative)
    return resolve_import_dir(config_value, default_relative)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import HK retailer CSV scrapes into SQLite.")
    parser.add_argument(
        "--hktvmall-dir",
        type=Path,
        default=None,
        help=f"HKTVmall CSV folder (default: {DEFAULT_HKTVMALL_IMPORT_DIR} under project root)",
    )
    parser.add_argument(
        "--shopify-dir",
        type=Path,
        default=None,
        help=f"Shopify retailer CSV folder (default: {DEFAULT_SHOPIFY_IMPORT_DIR} under project root)",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override catalog DB path (default: HK_CATALOG_DB_PATH from .env)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = get_settings()
    hktvmall_dir = _resolve_arg_or_config(
        args.hktvmall_dir,
        cfg.hk_catalog_import_hktvmall_dir,
        DEFAULT_HKTVMALL_IMPORT_DIR,
    )
    shopify_dir = _resolve_arg_or_config(
        args.shopify_dir,
        cfg.hk_catalog_import_shopify_dir,
        DEFAULT_SHOPIFY_IMPORT_DIR,
    )

    log.info("HKTVmall import dir: %s", hktvmall_dir)
    log.info("Shopify import dir:  %s", shopify_dir)

    listings = []
    hktv_csv_count = 0
    shopify_csv_count = 0

    if hktvmall_dir.is_dir():
        hktv_csv_count = len(list(hktvmall_dir.glob("*.csv")))
        if hktv_csv_count:
            hktv = import_hktvmall_dir(hktvmall_dir)
            log.info("Parsed %d HKTVmall listing(s) from %s", len(hktv), hktvmall_dir)
            listings.extend(hktv)
        else:
            log.warning("No *.csv files in HKTVmall dir: %s", hktvmall_dir)
    else:
        log.warning("HKTVmall directory not found (skipped): %s", hktvmall_dir)

    if shopify_dir.is_dir():
        shopify_csv_count = len(list(shopify_dir.glob("*.csv")))
        if shopify_csv_count:
            shopify = import_shopify_dir(shopify_dir)
            log.info("Parsed %d Shopify listing(s) from %s", len(shopify), shopify_dir)
            listings.extend(shopify)
        else:
            log.warning("No *.csv files in Shopify dir: %s", shopify_dir)
    else:
        log.warning("Shopify directory not found (skipped): %s", shopify_dir)

    if not listings:
        log.error(
            "No listings parsed. Copy CSV scrapes into:\n"
            "  %s\n"
            "  %s\n"
            "Or set HK_CATALOG_IMPORT_HKTVMALL_DIR / HK_CATALOG_IMPORT_SHOPIFY_DIR in .env",
            CATALOG_ROOT / DEFAULT_HKTVMALL_IMPORT_DIR,
            CATALOG_ROOT / DEFAULT_SHOPIFY_IMPORT_DIR,
        )
        return 1

    by_url: dict[str, object] = {}
    for item in listings:
        by_url[item.product_url] = item
    unique = list(by_url.values())

    db_path = args.db_path or Path(cfg.hk_catalog_db_path)
    if not db_path.is_absolute():
        db_path = CATALOG_ROOT / db_path

    store = HKCatalogStore(db_path=db_path, enabled=True)
    written = store.upsert_listings(unique)

    barcode_rows = sum(1 for item in unique if item.barcode)
    log.info(
        "Imported %d unique listing(s) into %s (%d with barcode index)",
        written,
        db_path,
        barcode_rows,
    )

    import src.catalog.store as catalog_store_module
    catalog_store_module._store = None

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
