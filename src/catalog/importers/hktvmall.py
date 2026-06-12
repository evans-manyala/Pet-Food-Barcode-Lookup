"""Import HKTVmall worksheet CSV exports."""

from __future__ import annotations

import csv
from pathlib import Path

from ..models import CatalogListing
from ..normalize import (
    extract_barcode_from_hktv_url,
    normalize_title,
    pick_lowest_price_hkd,
)


def _field(row: dict, *names: str) -> str:
    for name in names:
        value = (row.get(name) or "").strip()
        if value:
            return value
    return ""


def _parse_brand_from_title(title: str) -> str:
    if " - " in title:
        return title.split(" - ", 1)[0].strip()
    return ""


def _parse_in_stock(description: str) -> bool | None:
    text = (description or "").lower()
    if any(x in text for x in ["sold out", "缺貨", "售罄", "out of stock"]):
        return False
    return None


def parse_hktvmall_csv(path: Path) -> list[CatalogListing]:
    listings: list[CatalogListing] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            product_url = _field(row, "Product_URL_ZH", "Product_URL", "Product URL")
            if not product_url or "hktvmall.com" not in product_url:
                continue

            title_zh = _field(row, "SKU_Title_ZH", "SKU_Title")
            title_en = _field(row, "SKU_Title_EN", "SKU_Title")
            if not title_en and title_zh:
                title_en = title_zh

            brand = _parse_brand_from_title(title_en) or _parse_brand_from_title(title_zh)
            barcode, barcode_source = extract_barcode_from_hktv_url(product_url)

            price_hkd, price_value = pick_lowest_price_hkd(
                _field(row, "Special_Price2"),
                _field(row, "Special_Price1"),
            )

            description = _field(row, "Description_EN", "Description_ZH", "Description")
            scraped_at = _field(row, "Date_SCrapped", "Date Scrapped", "Date_Scrapped")

            listings.append(CatalogListing(
                source="hktvmall",
                product_url=product_url,
                title_en=title_en,
                title_zh=title_zh,
                title_norm=normalize_title(brand, title_en, title_zh),
                brand=brand,
                barcode=barcode,
                barcode_source=barcode_source or None,
                price_hkd=price_hkd,
                price_value=price_value,
                in_stock=_parse_in_stock(description),
                category=_field(row, "Product_Category"),
                description=description[:4000],
                seller_name=_field(row, "Seller_NameEN", "Seller_Name", "Seller_NameZH"),
                scraped_at=scraped_at,
            ))
    return listings


def import_hktvmall_dir(directory: Path) -> list[CatalogListing]:
    listings: list[CatalogListing] = []
    for path in sorted(directory.glob("*.csv")):
        parsed = parse_hktvmall_csv(path)
        listings.extend(parsed)
    return listings
