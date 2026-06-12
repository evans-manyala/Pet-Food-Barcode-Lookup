"""Merge and enrich catalog listings before SQLite import."""

from __future__ import annotations

import logging
from dataclasses import replace
from urllib.parse import urlparse

from .models import CatalogListing
from .normalize import extract_barcode_from_hktv_url, is_hktv_multipack_url, normalize_barcode

log = logging.getLogger(__name__)


def normalize_catalog_url(url: str | None) -> str:
    """Canonical URL key for deduping Shopify variants and master-scrape rows."""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower().removeprefix("www.")
    path = (parsed.path or "").rstrip("/").lower()
    return f"{host}{path}"


def _barcode_from_listing(listing: CatalogListing) -> tuple[str | None, str | None]:
    if is_hktv_multipack_url(listing.product_url):
        return None, None
    if listing.barcode:
        return listing.barcode, listing.barcode_source
    url_barcode, url_source = extract_barcode_from_hktv_url(listing.product_url)
    if url_barcode:
        return url_barcode, url_source
    return None, None


def _merge_pair(keep: CatalogListing, other: CatalogListing) -> CatalogListing:
    """Prefer barcode-rich rows; backfill missing fields from the other row."""
    keep_barcode, keep_source = _barcode_from_listing(keep)
    other_barcode, other_source = _barcode_from_listing(other)

    barcode = keep_barcode or other_barcode
    barcode_source = keep_source or other_source

    # Prefer master-scrape / override sources for barcode provenance.
    if other_barcode and other.source in {"petsorder", "legopet", "petmarket", "wnp", "override"}:
        if not keep_barcode or keep.source not in {"petsorder", "legopet", "petmarket", "wnp", "override"}:
            barcode = other_barcode
            barcode_source = other_source

    return CatalogListing(
        source=keep.source or other.source,
        product_url=keep.product_url or other.product_url,
        title_en=keep.title_en or other.title_en,
        title_zh=keep.title_zh or other.title_zh,
        title_norm=keep.title_norm or other.title_norm,
        brand=keep.brand or other.brand,
        barcode=barcode,
        barcode_source=barcode_source,
        price_hkd=keep.price_hkd or other.price_hkd,
        price_value=keep.price_value if keep.price_value is not None else other.price_value,
        in_stock=keep.in_stock if keep.in_stock is not None else other.in_stock,
        image_url=keep.image_url or other.image_url,
        category=keep.category or other.category,
        description=keep.description or other.description,
        seller_name=keep.seller_name or other.seller_name,
        scraped_at=keep.scraped_at or other.scraped_at,
    )


def dedupe_and_enrich_listings(listings: list[CatalogListing]) -> list[CatalogListing]:
    """
    Collapse duplicate product URLs and propagate barcodes across import sources.

    Shopify exports often omit barcodes while master-scrape / HKTV URL rows carry them.
    """
    by_url: dict[str, CatalogListing] = {}
    merged = 0
    barcodes_added = 0

    for item in listings:
        key = normalize_catalog_url(item.product_url)
        if not key:
            continue
        existing = by_url.get(key)
        if existing is None:
            barcode, barcode_source = _barcode_from_listing(item)
            if barcode and not item.barcode:
                item = replace(item, barcode=barcode, barcode_source=barcode_source)
                barcodes_added += 1
            by_url[key] = item
            continue

        had_barcode = bool(existing.barcode)
        merged_item = _merge_pair(existing, item)
        if merged_item.barcode and not had_barcode:
            barcodes_added += 1
        by_url[key] = merged_item
        merged += 1

    if merged:
        log.info("Merged %d duplicate catalog URL(s) during import", merged)
    if barcodes_added:
        log.info("Attached barcodes to %d catalog listing(s) during import", barcodes_added)

    return list(by_url.values())
