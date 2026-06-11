"""Text, price, and barcode normalization for the local HK catalog."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_RETAILER_DISPLAY = {
    "hktvmall.com": "HKTVmall",
    "www.hktvmall.com": "HKTVmall",
    "vetopia.com.hk": "Vetopia",
    "www.vetopia.com.hk": "Vetopia",
    "wnp.com.hk": "WnP",
    "www.wnp.com.hk": "WnP",
    "zh.wnp.com.hk": "WnP",
    "pettington.com": "Pettington",
    "www.pettington.com": "Pettington",
    "q-pets.com": "QPets",
    "www.q-pets.com": "QPets",
    "petsorder.com.hk": "PetsOrder",
    "www.petsorder.com.hk": "PetsOrder",
    "eshop.legopet.com.hk": "Lego Pet",
    "legopet.com.hk": "Lego Pet",
    "petmarket.com.hk": "PetMarket",
    "www.petmarket.com.hk": "PetMarket",
}


def normalize_barcode(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def barcode_variants(barcode: str) -> list[str]:
    """UPC/EAN leading-zero variants (mirrors llm_searcher logic)."""
    digits = normalize_barcode(barcode)
    variants = {digits}
    if len(digits) == 11:
        variants.add("0" + digits)
    if len(digits) == 12:
        variants.add("0" + digits)
        if digits.startswith("0"):
            variants.add(digits[1:])
    if len(digits) == 13 and digits.startswith("0"):
        variants.add(digits[1:])
        if digits.startswith("00"):
            variants.add(digits[2:])
    return [v for v in variants if v]


def normalize_match_text(value: str | None) -> str:
    value = (value or "").lower()
    value = re.sub(r"&amp;", "&", value)
    value = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_title(brand: str, title_en: str, title_zh: str) -> str:
    parts = [brand or "", title_en or "", title_zh or ""]
    return normalize_match_text(" ".join(p for p in parts if p))


_GENERIC_STOPWORDS = frozenset({
    "pet", "food", "cat", "dog", "for", "with", "and", "the", "dry", "wet",
    "can", "canned", "formula", "recipe", "adult", "kitten", "puppy",
    "hong", "kong", "hk", "size", "pack", "box", "bag",
})


def identity_tokens(product_name: str | None, brand: str | None) -> list[str]:
    text = normalize_match_text(f"{brand or ''} {product_name or ''}")
    tokens: list[str] = []
    for tok in re.findall(r"[a-z0-9]{3,}", text):
        if tok not in _GENERIC_STOPWORDS and tok not in tokens:
            tokens.append(tok)
    for tok in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if tok not in tokens:
            tokens.append(tok)
    return tokens[:18]


def extract_barcode_from_hktv_url(url: str | None) -> tuple[str | None, str]:
    """Return (barcode_digits, source_label) when EAN is embedded in an HKTV URL."""
    if not url:
        return None, ""
    patterns = (
        r"/_S_YX(\d{12,14})",
        r"/_S_(\d{12,14})(?:_H)?(?:/|$)",
        r"/p/[^/]+_S_(\d{12,14})",
    )
    for pattern in patterns:
        match = re.search(pattern, url, flags=re.I)
        if match:
            return match.group(1), "url"
    return None, ""


def parse_price_value(raw: str | None) -> float | None:
    if not raw:
        return None
    text = str(raw).strip()
    text = re.sub(r"^(HK\$|HKD|\$)\s*", "", text, flags=re.I)
    text = text.replace(",", "").strip()
    try:
        val = float(text)
    except ValueError:
        return None
    if 1 <= val <= 10000:
        return val
    return None


def format_price_hkd(value: float | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = parse_price_value(value)
        if parsed is None:
            return None
        value = parsed
    return f"HK${float(value):.2f}"


def pick_lowest_price_hkd(*values: str | float | None) -> tuple[str | None, float | None]:
    best_val: float | None = None
    for raw in values:
        val = raw if isinstance(raw, (int, float)) else parse_price_value(str(raw or ""))
        if val is None:
            continue
        if best_val is None or val < best_val:
            best_val = val
    if best_val is None:
        return None, None
    return format_price_hkd(best_val), best_val


def retailer_display_name(product_url: str, source: str, seller_name: str = "") -> str:
    host = urlparse(product_url or "").netloc.lower().lstrip("www.")
    if host in _RETAILER_DISPLAY:
        return _RETAILER_DISPLAY[host]
    if seller_name:
        return seller_name.strip()
    return source.replace("_", " ").title() or host or "Unknown"
