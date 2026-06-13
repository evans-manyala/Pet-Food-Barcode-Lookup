#!/usr/bin/env python3
"""
Patch Redis + Pinecone for one barcode without a live search.

Usage (on VM or locally):
  python scripts/patch_barcode_cache.py 052742703701
  python scripts/patch_barcode_cache.py 052742703701 --apply-override
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.catalog.overrides import get_barcode_override, override_to_retailer_dicts
from src.llm_searcher import ProductSearcher
from src.models import ProductInfo, RetailerListing
from src.pinecone_store import PineconeStore
from src.redis_cache import RedisCache


def _merge_retailers(existing: list[RetailerListing], new: list[RetailerListing]) -> list[RetailerListing]:
    by_url = {r.url: r for r in existing}
    for listing in new:
        by_url[listing.url] = listing
    return sorted(
        by_url.values(),
        key=lambda r: float("".join(ch for ch in (r.price_hkd or "") if ch.isdigit() or ch == ".") or "9999"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch cached product in Redis and Pinecone.")
    parser.add_argument("barcode", help="EAN/UPC barcode")
    parser.add_argument(
        "--apply-override",
        action="store_true",
        help="Merge validated retailers from data/barcode_overrides.json",
    )
    args = parser.parse_args()
    barcode = args.barcode.strip()

    redis = RedisCache()
    pinecone = PineconeStore()
    product = redis.get(barcode) or pinecone.fetch_by_barcode(barcode)

    if not product:
        print(f"No cached product for {barcode} in Redis or Pinecone.")
        return 1

    if args.apply_override:
        override = get_barcode_override(barcode)
        if not override:
            print(f"No override entry for {barcode}.")
            return 1
        searcher = ProductSearcher()
        candidates = override_to_retailer_dicts(override)
        validated = searcher._validate_retailers(
            candidates,
            barcode,
            product.product_name,
            product.brand or "",
            "",
        )
        if not validated:
            print("Override retailers failed validation.")
            return 1
        product = product.model_copy(update={"hk_retailers": _merge_retailers(product.hk_retailers, validated)})

    if redis.is_available:
        ok = redis.set(product)
        print(f"Redis: {'OK' if ok else 'FAILED'}")
    else:
        print("Redis: unavailable")

    if pinecone.is_available:
        ok = pinecone.upsert(product)
        print(f"Pinecone: {'OK' if ok else 'FAILED'}")
    else:
        print("Pinecone: unavailable")

    print(f"Retailers: {len(product.hk_retailers)}")
    for r in product.hk_retailers[:5]:
        print(f"  {r.retailer_name}  {r.price_hkd}  {r.url[:70]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
