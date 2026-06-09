"""Import Shopify-style retailer CSV exports (Vetopia, WnP, Pettington, QPets)."""

from __future__ import annotations

import csv
from pathlib import Path

from ..models import CatalogListing
from ..normalize import format_price_hkd, normalize_title, parse_price_value

_SOURCE_BY_FILENAME = {
    "vetopia.csv": "vetopia",
    "wnp.csv": "wnp",
    "pettington.csv": "pettington",
    "qpets.csv": "qpets",
    "q-pets.csv": "qpets",
}


def _split_field(value: str) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split("\n") if part.strip()]


def _parse_bool(value: str) -> bool | None:
    text = (value or "").strip().lower()
    if text in {"yes", "true", "1", "in stock"}:
        return True
    if text in {"no", "false", "0", "out of stock"}:
        return False
    return None


def _field(row: dict, *names: str) -> str:
    for name in names:
        value = (row.get(name) or "").strip()
        if value:
            return value
    return ""


def parse_shopify_csv(path: Path, source: str | None = None) -> list[CatalogListing]:
    source = source or _SOURCE_BY_FILENAME.get(path.name.lower(), path.stem.lower())
    listings: list[CatalogListing] = []

    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            title_en = _field(row, "Product Title_EN", "Product Title")
            title_zh = _field(row, "Product Title_ZH")
            brand = _field(row, "Brand")
            base_url = _field(
                row,
                "Product URL_EN",
                "Product URL",
                "Product URL_ZH",
            )
            if not base_url:
                continue

            variant_urls = _split_field(_field(row, "Variant URLs"))
            variant_titles = _split_field(_field(row, "Variant Titles"))
            regular_prices = _split_field(_field(row, "Regular Prices (HK$)"))
            sale_prices = _split_field(_field(row, "Sale Prices (HK$)"))
            skus = _split_field(_field(row, "SKUs"))
            availability = _split_field(_field(row, "Available"))

            count = max(
                len(variant_urls),
                len(regular_prices),
                len(sale_prices),
                1,
            )

            for index in range(count):
                url = variant_urls[index] if index < len(variant_urls) else base_url
                if not url:
                    continue

                sale_raw = sale_prices[index] if index < len(sale_prices) else ""
                regular_raw = regular_prices[index] if index < len(regular_prices) else ""
                price_value = parse_price_value(sale_raw) or parse_price_value(regular_raw)
                price_hkd = format_price_hkd(price_value)

                variant_title = variant_titles[index] if index < len(variant_titles) else ""
                full_title_en = title_en
                if variant_title and variant_title.upper() != "N/A":
                    full_title_en = f"{title_en} - {variant_title}".strip(" -")

                stock_raw = availability[index] if index < len(availability) else _field(row, "Available")

                listings.append(CatalogListing(
                    source=source,
                    product_url=url,
                    title_en=full_title_en,
                    title_zh=title_zh,
                    title_norm=normalize_title(brand, full_title_en, title_zh),
                    brand=brand,
                    barcode=None,
                    barcode_source=None,
                    price_hkd=price_hkd,
                    price_value=price_value,
                    in_stock=_parse_bool(stock_raw),
                    image_url=_field(row, "Main Image URL") or None,
                    category=_field(row, "Category", "Pet Type", "Product Type"),
                    description="",
                    seller_name=source,
                    scraped_at=_field(row, "Updated At", "Created At"),
                ))

    return listings


def import_shopify_dir(directory: Path) -> list[CatalogListing]:
    listings: list[CatalogListing] = []
    for path in sorted(directory.glob("*.csv")):
        listings.extend(parse_shopify_csv(path))
    return listings
