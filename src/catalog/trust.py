"""Trusted catalog listing rules (skip live validation when evidence is strong)."""

from __future__ import annotations

from .models import CatalogListing
from .normalize import barcode_variants, format_price_hkd


def catalog_price_freshness_note(listing: CatalogListing) -> str:
    if listing.scraped_at:
        return f"Price from catalog scrape ({listing.scraped_at}); verify on site."
    return "Price from local catalog; verify on site."


def is_trusted_hktv_barcode_listing(
    listing: CatalogListing,
    lookup_barcode: str,
) -> bool:
    """
    Trust HKTVmall URLs when the scrape ties an EAN to the product URL and
  includes an HK$ price — live page fetch often fails on HKTV bot protection.
    """
    if listing.source != "hktvmall":
        return False
    if listing.barcode_source != "url" or not listing.barcode:
        return False
    if not (listing.price_hkd or format_price_hkd(listing.price_value)):
        return False

    lookup_variants = set(barcode_variants(lookup_barcode))
    listing_variants = set(barcode_variants(listing.barcode))
    return bool(lookup_variants & listing_variants)
