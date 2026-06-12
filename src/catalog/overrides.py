"""Load version-controlled barcode override corrections."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import CatalogListing
from .normalize import format_price_hkd, normalize_title

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class BarcodeOverride:
    barcode: str
    product_name: str
    brand: str = ""
    target_animal: str = ""
    image_url: str = ""
    manufacturer_url: str = ""
    barcode_evidence: str = ""
    identity_confidence: str = "high"
    nutritional_info: dict[str, Any] = field(default_factory=dict)
    retailers: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # shopper-facing UI copy
    notes: str = ""  # internal ops / import notes — never shown in lookup UI


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def load_barcode_overrides(path: str | Path | None = None) -> dict[str, BarcodeOverride]:
    """Return overrides keyed by normalized barcode digits."""
    if path is None:
        from src.config import get_settings
        path = get_settings().hk_catalog_overrides_path

    file_path = _resolve_path(path)
    if not file_path.is_file():
        log.debug("Barcode overrides file not found: %s", file_path)
        return {}

    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Failed to read barcode overrides %s: %s", file_path, exc)
        return {}

    entries = raw.get("overrides") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        log.warning("Barcode overrides file has invalid format: %s", file_path)
        return {}

    out: dict[str, BarcodeOverride] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        barcode = "".join(ch for ch in str(item.get("barcode") or "") if ch.isdigit())
        product_name = (item.get("product_name") or "").strip()
        if not barcode or not product_name:
            continue
        out[barcode] = BarcodeOverride(
            barcode=barcode,
            product_name=product_name,
            brand=(item.get("brand") or "").strip(),
            target_animal=(item.get("target_animal") or "").strip(),
            image_url=(item.get("image_url") or "").strip(),
            manufacturer_url=(item.get("manufacturer_url") or "").strip(),
            barcode_evidence=(item.get("barcode_evidence") or "").strip(),
            identity_confidence=(item.get("identity_confidence") or "high").strip(),
            nutritional_info=dict(item.get("nutritional_info") or {}),
            retailers=list(item.get("retailers") or []),
            warnings=[
                str(w).strip()
                for w in (item.get("warnings") or [])
                if str(w).strip()
            ],
            notes=(item.get("notes") or "").strip(),
        )

    log.info("Loaded %d barcode override(s) from %s", len(out), file_path)
    return out


def get_barcode_override(barcode: str, path: str | Path | None = None) -> BarcodeOverride | None:
    digits = "".join(ch for ch in barcode if ch.isdigit())
    return load_barcode_overrides(path).get(digits)


def override_to_identity_dict(override: BarcodeOverride) -> dict:
    evidence_urls = [
        str(r.get("url") or "").strip()
        for r in override.retailers
        if r.get("url")
    ]
    return {
        "product_name": override.product_name,
        "brand": override.brand or None,
        "target_animal": override.target_animal or None,
        "manufacturer_url": override.manufacturer_url or None,
        "image_url": override.image_url or None,
        "nutritional_info": override.nutritional_info,
        "barcode_verified": True,
        "identity_confidence": override.identity_confidence,
        "evidence_urls": evidence_urls,
        "barcode_evidence": override.barcode_evidence or (
            f"Curated barcode override for {override.barcode}."
        ),
        "warnings": list(override.warnings),
    }


def override_to_catalog_listings(override: BarcodeOverride) -> list[CatalogListing]:
    """Convert override retailer rows into catalog listings for import."""
    now = datetime.now(timezone.utc).isoformat()
    listings: list[CatalogListing] = []
    for row in override.retailers:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        price_value = row.get("price_value")
        if price_value is None and row.get("price_hkd"):
            from .normalize import parse_price_value
            price_value = parse_price_value(str(row.get("price_hkd")))
        price_hkd = row.get("price_hkd") or format_price_hkd(price_value)
        source = (row.get("source") or "override").strip()
        seller = (row.get("seller_name") or row.get("retailer_name") or source).strip()
        listings.append(CatalogListing(
            source=source,
            product_url=url,
            title_en=override.product_name,
            title_zh="",
            title_norm=normalize_title(override.brand, override.product_name, ""),
            brand=override.brand,
            barcode=override.barcode,
            barcode_source="override",
            price_hkd=price_hkd,
            price_value=float(price_value) if price_value is not None else None,
            image_url=override.image_url or None,
            category=row.get("category") or "",
            description=override.notes,
            seller_name=seller,
            scraped_at=now,
        ))
    return listings


def overrides_to_catalog_listings(overrides: dict[str, BarcodeOverride] | None = None) -> list[CatalogListing]:
    if overrides is None:
        overrides = load_barcode_overrides()
    listings: list[CatalogListing] = []
    for override in overrides.values():
        listings.extend(override_to_catalog_listings(override))
    return listings


def override_to_retailer_dicts(override: BarcodeOverride) -> list[dict]:
    """Retailer candidate dicts compatible with ProductSearcher._validate_retailers()."""
    candidates: list[dict] = []
    for row in override.retailers:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        price = row.get("price_hkd") or format_price_hkd(row.get("price_value"))
        seller = (row.get("seller_name") or row.get("retailer_name") or row.get("source") or "HK").strip()
        candidates.append({
            "retailer_name": seller,
            "url": url,
            "price_hkd": price,
            "in_stock": row.get("in_stock", True),
            "notes": "Curated barcode override retailer.",
            "catalog_trusted": True,
            "catalog_source": row.get("source") or "override",
        })
    return candidates
