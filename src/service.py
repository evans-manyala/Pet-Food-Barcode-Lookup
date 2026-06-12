"""
service.py – Core barcode lookup pipeline (Redis → Pinecone → live search).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

from src.config import get_settings
from src.llm_searcher import ProductSearcher
from src.models import ProductInfo
from src.pinecone_store import PineconeStore
from src.redis_cache import RedisCache

log = logging.getLogger(__name__)

LookupSource = Literal["redis", "pinecone", "live_search", ""]


@dataclass
class LookupResult:
    product: Optional[ProductInfo]
    source: LookupSource = ""
    error: str = ""
    timings: dict | None = None
    catalog_stats: dict | None = None


def is_cache_safe(product: ProductInfo) -> bool:
    """Old cached records without verification metadata are not trusted."""
    return (
        bool(product.barcode_verified)
        and product.identity_confidence in {"high", "medium"}
        and bool(product.product_name)
        and product.product_name != "Unknown Product"
    )


def lookup_barcode(barcode: str, force_refresh: bool = False) -> LookupResult:
    """
    Lookup pipeline: Redis → Pinecone → Gemini web search.
    Returns a LookupResult with product, source layer, or error message.
    """
    cfg = get_settings()
    redis_cache = RedisCache()
    pinecone = PineconeStore()

    if not force_refresh and redis_cache.is_available:
        product = redis_cache.get(barcode)
        if product:
            if is_cache_safe(product):
                log.info("Cache hit: Redis for %s", barcode)
                return LookupResult(product=product, source="redis")
            log.warning("Ignoring unsafe Redis entry for %s", barcode)
            redis_cache.delete(barcode)

    if not force_refresh and pinecone.is_available:
        product = pinecone.fetch_by_barcode(barcode)
        if product:
            if is_cache_safe(product):
                if redis_cache.is_available:
                    redis_cache.set(product)
                log.info("Cache hit: Pinecone for %s", barcode)
                return LookupResult(product=product, source="pinecone")
            log.warning("Ignoring unsafe Pinecone entry for %s", barcode)

    log.info("Live web search for %s", barcode)
    searcher = ProductSearcher()
    product = searcher.search(barcode)
    timings = searcher.last_lookup_timings or None
    catalog_stats = searcher.last_catalog_stats or None

    if not is_cache_safe(product):
        return LookupResult(
            product=product,
            source="live_search",
            error=(
                "Product not safely identified. "
                "No strong source evidence links this barcode to a verified product."
            ),
            timings=timings,
            catalog_stats=catalog_stats,
        )

    if redis_cache.is_available:
        redis_cache.set(product)
        log.debug("Saved to Redis (TTL: %sh)", cfg.redis_ttl // 3600)

    if pinecone.is_available:
        pinecone.upsert(product)
        log.debug("Saved to Pinecone")

    return LookupResult(
        product=product,
        source="live_search",
        timings=timings,
        catalog_stats=catalog_stats,
    )
