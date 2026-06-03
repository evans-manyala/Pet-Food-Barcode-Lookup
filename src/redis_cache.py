"""
redis_cache.py – Redis-backed product cache.

Keys  : pet_food:<barcode>
Value : JSON-serialised ProductInfo
TTL   : configurable (default 24 h)
"""

from __future__ import annotations
import json
import logging
from typing import Optional

import redis

from .config import get_settings
from .models import ProductInfo

log = logging.getLogger(__name__)


class RedisCache:
    """Thin wrapper around redis.Redis for ProductInfo caching."""

    _KEY_PREFIX = "pet_food"

    def __init__(self) -> None:
        cfg = get_settings()
        self._ttl = cfg.redis_ttl
        try:
            self._r = redis.from_url(cfg.redis_url, decode_responses=True)
            self._r.ping()
            log.info("Redis connected: %s", cfg.redis_url)
            self._available = True
        except Exception as exc:
            log.warning(
                "Redis unavailable (%s). Cache will be skipped.", exc
            )
            self._available = False

    # ── Helpers ───────────────────────────────────────────────────────────

    def _key(self, barcode: str) -> str:
        return f"{self._KEY_PREFIX}:{barcode}"

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        return self._available

    def get(self, barcode: str) -> Optional[ProductInfo]:
        """Return cached ProductInfo or None on miss / error."""
        if not self._available:
            return None
        try:
            raw = self._r.get(self._key(barcode))
            if raw is None:
                log.debug("Redis cache MISS for barcode=%s", barcode)
                return None
            log.info("Redis cache HIT for barcode=%s", barcode)
            data = json.loads(raw)
            return ProductInfo(**data)
        except Exception as exc:
            log.warning("Redis GET error: %s", exc)
            return None

    def set(self, product: ProductInfo) -> bool:
        """Persist a ProductInfo object. Returns True on success."""
        if not self._available:
            return False
        try:
            key  = self._key(product.barcode)
            data = product.model_dump()
            self._r.setex(key, self._ttl, json.dumps(data))
            log.info(
                "Redis SET barcode=%s TTL=%ds", product.barcode, self._ttl
            )
            return True
        except Exception as exc:
            log.warning("Redis SET error: %s", exc)
            return False

    def delete(self, barcode: str) -> bool:
        """Manually invalidate a cached entry."""
        if not self._available:
            return False
        try:
            deleted = self._r.delete(self._key(barcode))
            log.info("Redis DELETE barcode=%s deleted=%s", barcode, bool(deleted))
            return bool(deleted)
        except Exception as exc:
            log.warning("Redis DELETE error: %s", exc)
            return False

    def ttl(self, barcode: str) -> int:
        """Return remaining TTL in seconds, or -2 if the key does not exist."""
        if not self._available:
            return -2
        try:
            return self._r.ttl(self._key(barcode))
        except Exception:
            return -2
