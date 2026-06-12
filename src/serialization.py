"""
serialization.py – Convert ProductInfo to API-friendly JSON payloads.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from src.models import ProductInfo
from src.user_warnings import warnings_for_ui


def _number_from_text(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _normalise_analysis_key(key: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", key or "").strip("_").lower()
    aliases = {
        "crude_protein_min": "protein",
        "crude_fat_min": "fat_content",
        "crude_fiber_max": "crude_fiber",
        "moisture_max": "moisture",
        "ash_max": "crude_ash",
        "crude_ash_max": "crude_ash",
    }
    return aliases.get(cleaned, cleaned)


def product_to_api_payload(product: ProductInfo, source: str = "") -> dict[str, Any]:
    ni = product.nutritional_info
    guaranteed_analysis: dict[str, Optional[float]] = {}
    if ni:
        guaranteed_analysis = {
            "protein": _number_from_text(ni.crude_protein_min),
            "fat_content": _number_from_text(ni.crude_fat_min),
            "crude_fiber": _number_from_text(ni.crude_fiber_max),
            "moisture": _number_from_text(ni.moisture_max),
            "crude_ash": _number_from_text(ni.ash_max),
            "calories": _number_from_text(ni.calories),
        }
        for key, value in (ni.other or {}).items():
            guaranteed_analysis[_normalise_analysis_key(key)] = _number_from_text(value)

    prices = []
    for retailer in product.hk_retailers:
        price = _number_from_text(retailer.price_hkd)
        prices.append({
            "store": retailer.retailer_name,
            "retailer_name": retailer.retailer_name,
            "price": price,
            "price_display": retailer.price_hkd,
            "currency": "HKD",
            "url": retailer.url,
            "in_stock": retailer.in_stock,
            "region": "HK",
            "notes": retailer.notes,
        })
    prices.sort(key=lambda item: item["price"] if item["price"] is not None else float("inf"))

    nutrition_display = []
    if ni:
        rows = [
            ("Crude Protein (min)", ni.crude_protein_min),
            ("Crude Fat (min)", ni.crude_fat_min),
            ("Crude Fiber (max)", ni.crude_fiber_max),
            ("Moisture (max)", ni.moisture_max),
            ("Ash (max)", ni.ash_max),
            ("Calories", ni.calories),
        ]
        for label, val in rows:
            if val:
                nutrition_display.append({"label": label, "value": val})
        for name, value in (ni.other or {}).items():
            nutrition_display.append({"label": name, "value": value})

    return {
        "barcode": product.barcode,
        "product_name": product.product_name,
        "title_en": product.product_name,
        "brand": product.brand,
        "target_animal": product.target_animal,
        "pet_type": product.target_animal,
        "manufacturer_url": product.manufacturer_url,
        "image_url": product.image_url,
        "image_display": {
            "width": 240,
            "height": 240,
            "object_fit": "contain",
        },
        "guaranteed_analysis": guaranteed_analysis,
        "nutrition_display": nutrition_display,
        "price_comparison": prices,
        "best_price": prices[0] if prices else None,
        "source_urls": product.evidence_urls,
        "barcode_verified": product.barcode_verified,
        "identity_confidence": product.identity_confidence,
        "barcode_evidence": product.barcode_evidence,
        "warnings": warnings_for_ui(product.warnings),
        "source": source,
    }
