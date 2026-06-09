"""Canonical local HK retailer catalog record."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class CatalogListing:
    source: str
    product_url: str
    title_en: str = ""
    title_zh: str = ""
    title_norm: str = ""
    brand: str = ""
    barcode: Optional[str] = None
    barcode_source: Optional[str] = None
    price_hkd: Optional[str] = None
    price_value: Optional[float] = None
    in_stock: Optional[bool] = None
    image_url: Optional[str] = None
    category: str = ""
    description: str = ""
    seller_name: str = ""
    scraped_at: str = ""
