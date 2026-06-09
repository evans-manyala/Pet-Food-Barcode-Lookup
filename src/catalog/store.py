"""SQLite-backed local HK retailer catalog."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config import get_settings

from .match import rank_listings_for_product
from .models import CatalogListing
from .normalize import (
    barcode_variants,
    format_price_hkd,
    identity_tokens,
    normalize_match_text,
    retailer_display_name,
)
from .trust import catalog_price_freshness_note, is_trusted_hktv_barcode_listing

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    barcode TEXT,
    barcode_source TEXT,
    brand TEXT,
    title_en TEXT,
    title_zh TEXT,
    title_norm TEXT,
    product_url TEXT NOT NULL UNIQUE,
    price_hkd TEXT,
    price_value REAL,
    in_stock INTEGER,
    image_url TEXT,
    category TEXT,
    description TEXT,
    seller_name TEXT,
    scraped_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_catalog_barcode ON catalog_listings(barcode);
CREATE INDEX IF NOT EXISTS idx_catalog_brand ON catalog_listings(brand);
CREATE INDEX IF NOT EXISTS idx_catalog_title_norm ON catalog_listings(title_norm);
CREATE TABLE IF NOT EXISTS catalog_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class HKCatalogStore:
    """Read-only catalog access for lookup; writes happen via import script."""

    def __init__(self, db_path: Path, enabled: bool = True) -> None:
        self._enabled = enabled
        self._db_path = db_path
        self._available = False
        if not enabled:
            log.info("HK catalog disabled via config.")
            return
        if not db_path.exists():
            log.info(
                "HK catalog DB not found at %s (run scripts/import_hk_catalog.py).",
                db_path,
            )
            return
        try:
            self._init_schema()
            self._available = True
            count = self._count_rows()
            log.info("HK catalog ready: %s (%d listings)", db_path, count)
        except Exception as exc:
            log.warning("HK catalog init failed: %s", exc)

    @property
    def is_available(self) -> bool:
        return self._available

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _count_rows(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM catalog_listings").fetchone()
            return int(row["c"]) if row else 0

    def upsert_listings(self, listings: list[CatalogListing]) -> int:
        """Insert or replace listings (used by import script)."""
        if not listings:
            return 0
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        sql = """
        INSERT INTO catalog_listings (
            source, barcode, barcode_source, brand, title_en, title_zh, title_norm,
            product_url, price_hkd, price_value, in_stock, image_url, category,
            description, seller_name, scraped_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(product_url) DO UPDATE SET
            source=excluded.source,
            barcode=excluded.barcode,
            barcode_source=excluded.barcode_source,
            brand=excluded.brand,
            title_en=excluded.title_en,
            title_zh=excluded.title_zh,
            title_norm=excluded.title_norm,
            price_hkd=excluded.price_hkd,
            price_value=excluded.price_value,
            in_stock=excluded.in_stock,
            image_url=excluded.image_url,
            category=excluded.category,
            description=excluded.description,
            seller_name=excluded.seller_name,
            scraped_at=excluded.scraped_at
        """
        rows = [
            (
                item.source,
                item.barcode,
                item.barcode_source,
                item.brand,
                item.title_en,
                item.title_zh,
                item.title_norm,
                item.product_url,
                item.price_hkd,
                item.price_value,
                None if item.in_stock is None else int(item.in_stock),
                item.image_url,
                item.category,
                item.description,
                item.seller_name,
                item.scraped_at,
            )
            for item in listings
        ]
        with self._connect() as conn:
            conn.executemany(sql, rows)
            conn.execute(
                """
                INSERT INTO catalog_metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                ("last_import_at", datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        self._available = True
        return len(rows)

    def get_stats(self) -> dict:
        if not self._available:
            return {
                "enabled": self._enabled,
                "available": False,
                "listings": 0,
                "barcode_indexed": 0,
                "last_import_at": None,
            }
        with self._connect() as conn:
            listings = conn.execute("SELECT COUNT(*) AS c FROM catalog_listings").fetchone()["c"]
            barcode_indexed = conn.execute(
                "SELECT COUNT(*) AS c FROM catalog_listings WHERE barcode IS NOT NULL AND barcode != ''"
            ).fetchone()["c"]
            meta = conn.execute(
                "SELECT value FROM catalog_metadata WHERE key='last_import_at'"
            ).fetchone()
        return {
            "enabled": self._enabled,
            "available": True,
            "listings": int(listings),
            "barcode_indexed": int(barcode_indexed),
            "last_import_at": meta["value"] if meta else None,
        }

    def find_by_barcode(self, barcode: str, limit: int = 20) -> list[CatalogListing]:
        if not self._available:
            return []
        variants = barcode_variants(barcode)
        if not variants:
            return []
        placeholders = ",".join("?" for _ in variants)
        sql = f"""
        SELECT * FROM catalog_listings
        WHERE barcode IN ({placeholders})
        ORDER BY (price_value IS NULL), price_value ASC
        LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, [*variants, limit]).fetchall()
        return [self._row_to_listing(row) for row in rows]

    def find_retailers_by_product(
        self,
        brand: str,
        product_name: str,
        barcode: str = "",
        limit: int = 15,
    ) -> list[CatalogListing]:
        """Fuzzy retailer discovery after identity is verified."""
        if not self._available:
            return []

        by_url: dict[str, CatalogListing] = {}

        if barcode:
            for listing in self.find_by_barcode(barcode, limit=limit):
                by_url[listing.product_url] = listing

        brand_norm = normalize_match_text(brand)
        rows: list[sqlite3.Row] = []
        with self._connect() as conn:
            if brand_norm and len(brand_norm) >= 3:
                pattern = f"%{brand_norm}%"
                rows = conn.execute(
                    """
                    SELECT * FROM catalog_listings
                    WHERE brand LIKE ? OR title_norm LIKE ?
                    LIMIT 500
                    """,
                    (pattern, pattern),
                ).fetchall()
            else:
                tokens = identity_tokens(product_name, brand)
                if tokens:
                    pattern = f"%{tokens[0]}%"
                    rows = conn.execute(
                        "SELECT * FROM catalog_listings WHERE title_norm LIKE ? LIMIT 500",
                        (pattern,),
                    ).fetchall()

        candidates = [self._row_to_listing(row) for row in rows]
        ranked = rank_listings_for_product(candidates, brand, product_name, limit=limit)
        for listing in ranked:
            by_url.setdefault(listing.product_url, listing)

        return list(by_url.values())[:limit]

    def format_identity_evidence(
        self,
        barcode: str,
        listings: list[CatalogListing],
    ) -> tuple[str, list[str]]:
        """Build research text for Phase 1 Gemini extraction."""
        if not listings:
            return "", []

        parts = [
            f"Local HK retailer catalog: exact barcode match for {barcode}.",
            (
                f"Found {len(listings)} listing(s) with barcode embedded in "
                "source URL or catalog index."
            ),
        ]
        urls: list[str] = []
        for listing in listings[:8]:
            title = listing.title_en or listing.title_zh or "Unknown product"
            urls.append(listing.product_url)
            parts.append(
                "\n".join([
                    f"Source: {listing.source}",
                    (
                        "Retailer: "
                        f"{retailer_display_name(listing.product_url, listing.source, listing.seller_name)}"
                    ),
                    f"Brand: {listing.brand or '—'}",
                    f"Product: {title}",
                    (
                        f"Barcode: {listing.barcode or barcode} "
                        f"(source: {listing.barcode_source or 'catalog'})"
                    ),
                    f"Price: {listing.price_hkd or '—'}",
                    f"URL: {listing.product_url}",
                    (
                        f"Evidence: product URL explicitly references barcode "
                        f"{listing.barcode or barcode}."
                    ),
                ])
            )
            if listing.description:
                snippet = listing.description.replace("\n", " ").strip()[:600]
                parts.append(f"Description excerpt: {snippet}")

        return "\n\n".join(parts), list(dict.fromkeys(urls))

    def to_retailer_candidates(
        self,
        listings: list[CatalogListing],
        lookup_barcode: str = "",
    ) -> list[dict]:
        """Convert catalog rows into retailer dicts for _validate_retailers()."""
        candidates: list[dict] = []
        for listing in listings:
            if not listing.product_url:
                continue
            price = listing.price_hkd or format_price_hkd(listing.price_value)
            trusted = is_trusted_hktv_barcode_listing(listing, lookup_barcode)
            freshness = catalog_price_freshness_note(listing)
            scraped = f", scraped {listing.scraped_at}" if listing.scraped_at else ""
            notes = f"Local HK catalog ({listing.source}{scraped}). {freshness}"
            if trusted:
                notes += " Trusted catalog URL (barcode in HKTV product link)."
            candidates.append({
                "retailer_name": retailer_display_name(
                    listing.product_url, listing.source, listing.seller_name,
                ),
                "url": listing.product_url,
                "price_hkd": price,
                "in_stock": listing.in_stock,
                "notes": notes.strip(),
                "catalog_trusted": trusted,
                "catalog_source": listing.source,
            })
        return candidates

    @staticmethod
    def _row_to_listing(row: sqlite3.Row) -> CatalogListing:
        in_stock = row["in_stock"]
        return CatalogListing(
            source=row["source"] or "",
            product_url=row["product_url"] or "",
            title_en=row["title_en"] or "",
            title_zh=row["title_zh"] or "",
            title_norm=row["title_norm"] or "",
            brand=row["brand"] or "",
            barcode=row["barcode"],
            barcode_source=row["barcode_source"],
            price_hkd=row["price_hkd"],
            price_value=row["price_value"],
            in_stock=None if in_stock is None else bool(in_stock),
            image_url=row["image_url"],
            category=row["category"] or "",
            description=row["description"] or "",
            seller_name=row["seller_name"] or "",
            scraped_at=row["scraped_at"] or "",
        )


_store: Optional[HKCatalogStore] = None


def get_catalog_store() -> HKCatalogStore:
    global _store
    if _store is None:
        cfg = get_settings()
        db_path = Path(cfg.hk_catalog_db_path)
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path
        _store = HKCatalogStore(db_path=db_path, enabled=cfg.hk_catalog_enabled)
    return _store
