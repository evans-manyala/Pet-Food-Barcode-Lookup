"""Product matching helpers for catalog retailer discovery."""

from __future__ import annotations

from .models import CatalogListing
from .normalize import identity_tokens, normalize_match_text


def score_listing_for_product(
    listing: CatalogListing,
    brand: str,
    product_name: str,
) -> int:
    """Higher score = better fuzzy match on verified product identity."""
    query_tokens = identity_tokens(product_name, brand)
    if not query_tokens:
        return 0

    haystack = normalize_match_text(
        f"{listing.brand} {listing.title_en} {listing.title_zh} {listing.title_norm}"
    )
    if not haystack:
        return 0

    brand_norm = normalize_match_text(brand)
    score = 0
    if brand_norm and len(brand_norm) >= 3 and brand_norm in haystack:
        score += 4

    hits = sum(1 for token in query_tokens if token in haystack)
    required = 2 if len(query_tokens) <= 5 else 3
    if hits < required:
        return 0

    score += hits * 2
    return score


def rank_listings_for_product(
    listings: list[CatalogListing],
    brand: str,
    product_name: str,
    limit: int = 15,
) -> list[CatalogListing]:
    scored: list[tuple[int, CatalogListing]] = []
    for listing in listings:
        score = score_listing_for_product(listing, brand, product_name)
        if score > 0:
            scored.append((score, listing))
    scored.sort(key=lambda item: (-item[0], item[1].price_value or 99999))
    return [listing for _, listing in scored[:limit]]
