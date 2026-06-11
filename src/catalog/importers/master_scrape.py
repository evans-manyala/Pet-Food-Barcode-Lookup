"""Import unified HK retailer barcode scrape CSV (PetsOrder, Lego Pet, PetMarket, WnP)."""

from __future__ import annotations

import csv
import re
from pathlib import Path

from ..models import CatalogListing
from ..normalize import format_price_hkd, normalize_barcode, normalize_title, parse_price_value

_STORE_SOURCE = {
    "petsorder": "petsorder",
    "lego pet": "legopet",
    "petmarket": "petmarket",
    "wnp": "wnp",
}


def _field(row: dict, *names: str) -> str:
    for name in names:
        value = (row.get(name) or "").strip()
        if value:
            return value
    return ""


def _is_gtin(digits: str) -> bool:
    return len(digits) in (8, 12, 13, 14)


def _pick_product_barcode(row: dict) -> tuple[str | None, str | None]:
    """
    Choose the product barcode for catalog indexing.

    This scrape often stores an internal variant id in ``Barcode`` and the real
    EAN/UPC in ``SKU`` (e.g. Naturea beef wet food: Barcode=488415285920,
    SKU=5600874373474).
    """
    barcode = normalize_barcode(_field(row, "Barcode"))
    sku = normalize_barcode(_field(row, "SKU"))

    if barcode and sku and barcode == sku and _is_gtin(barcode):
        return barcode, "csv_barcode"

    if _is_gtin(sku) and (not _is_gtin(barcode) or len(sku) >= len(barcode)):
        if sku != barcode:
            return sku, "sku"

    if _is_gtin(barcode):
        return barcode, "csv_barcode"
    if _is_gtin(sku):
        return sku, "sku"

    value = barcode or sku or None
    if not value:
        return None, None
    source = "csv_barcode" if barcode else "sku"
    return value, source


def _parse_store_source(store: str) -> str:
    key = (store or "").strip().lower()
    return _STORE_SOURCE.get(key, key.replace(" ", "_") or "master_scrape")


def _adjust_petmarket_price(value: float | None, raw: str) -> float | None:
    """
    Correct PetMarket scrape prices that are 10× too high.

    The site shows prices like ``$ 13.0`` but the scrape CSV often stores
    ``HK$130.0`` (decimal digit appended). Same pattern for $11 → 110, $18 → 180.
    """
    if value is None:
        return None

    text = re.sub(r"^(HK\$|HKD|\$)\s*", "", (raw or "").strip(), flags=re.I)
    text = text.replace(",", "")
    if not re.fullmatch(r"\d+\.0", text):
        return value
    if value < 100:
        return value

    corrected = value / 10
    if 8 <= corrected <= 99:
        return corrected
    return value


def _pick_price(row: dict, source: str) -> tuple[float | None, str | None]:
    sale_raw = _field(row, "Sale Price (HK$)")
    regular_raw = _field(row, "Regular Price (HK$)")
    for raw in (sale_raw, regular_raw):
        if raw and raw.upper() != "N/A":
            value = parse_price_value(raw)
            if value is not None:
                if source == "petmarket":
                    value = _adjust_petmarket_price(value, raw)
                return value, format_price_hkd(value)
    return None, None


def parse_master_scrape_csv(path: Path) -> list[CatalogListing]:
    listings: list[CatalogListing] = []

    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            product_url = _field(row, "Product URL")
            if not product_url:
                continue

            title = _field(row, "Product Title")
            brand = _field(row, "Brand")
            store = _field(row, "Store")
            source = _parse_store_source(store)

            barcode, barcode_source = _pick_product_barcode(row)
            price_value, price_hkd = _pick_price(row, source)

            variant = _field(row, "Variant Title")
            full_title = title
            if variant and variant.upper() not in {"DEFAULT", "DEFAULT TITLE", "N/A"}:
                full_title = f"{title} ({variant})".strip()

            listings.append(CatalogListing(
                source=source,
                product_url=product_url,
                title_en=full_title,
                title_zh="",
                title_norm=normalize_title(brand, full_title, ""),
                brand=brand,
                barcode=barcode,
                barcode_source=barcode_source,
                price_hkd=price_hkd,
                price_value=price_value,
                in_stock=None,
                category=_field(row, "Food Category"),
                description="",
                seller_name=store or source,
                scraped_at="",
            ))

    return listings


def import_master_scrape_dir(directory: Path) -> list[CatalogListing]:
    listings: list[CatalogListing] = []
    for path in sorted(directory.glob("*.csv")):
        listings.extend(parse_master_scrape_csv(path))
    return listings
