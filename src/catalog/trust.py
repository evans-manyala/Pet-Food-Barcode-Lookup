"""Trusted catalog listing rules (skip live validation when evidence is strong)."""

from __future__ import annotations

from .models import CatalogListing
from .normalize import barcode_variants, format_price_hkd

_BARCODE_SCRAPE_SOURCES = frozenset({
    "petsorder",
    "legopet",
    "petmarket",
    "wnp",
})


def catalog_price_freshness_note(listing: CatalogListing) -> str:
    if listing.scraped_at:
        return f"Price from catalog scrape ({listing.scraped_at}); verify on site."
    return "Price from local catalog; verify on site."


def _barcode_matches_lookup(listing: CatalogListing, lookup_barcode: str) -> bool:
    if not listing.barcode:
        return False
    lookup_variants = set(barcode_variants(lookup_barcode))
    listing_variants = set(barcode_variants(listing.barcode))
    return bool(lookup_variants & listing_variants)


def _has_catalog_price(listing: CatalogListing) -> bool:
    return bool(listing.price_hkd or format_price_hkd(listing.price_value))


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
    if not _has_catalog_price(listing):
        return False
    return _barcode_matches_lookup(listing, lookup_barcode)


def is_trusted_catalog_barcode_listing(
    listing: CatalogListing,
    lookup_barcode: str,
) -> bool:
    """
    Trust scraped catalog rows with indexed barcode + HK$ price.

    Covers HKTV URL barcodes and master-scrape retailers (PetsOrder, Lego Pet,
    PetMarket, WnP) where live HTTP validation may fail but scrape data is strong.
    """
    if not _has_catalog_price(listing):
        return False
    if not _barcode_matches_lookup(listing, lookup_barcode):
        return False

    if listing.source == "hktvmall" and listing.barcode_source == "url":
        return True

    if (
        listing.source in _BARCODE_SCRAPE_SOURCES
        and listing.barcode_source in {"csv_barcode", "sku"}
    ):
        return True

    if listing.barcode_source in {"manual", "override"}:
        return True

    return False
