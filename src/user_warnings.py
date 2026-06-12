"""
User-facing warning copy for the lookup UI.

Keep operational / pipeline messages in logs and override `notes` fields.
Only shopper-friendly strings should reach ProductInfo.warnings and the API.
"""

from __future__ import annotations

import re

# Substrings that mark warnings meant for logs/ops, not the public UI.
_INTERNAL_WARNING_MARKERS = (
    "result not cached",
    "not saved to cache",
    "vector store",
    "gemini returned",
    "malformed/truncated json",
    "do not merge",
    "shopline pages list",
    "use 2kg prices only",
    "urls are shown for this barcode",
    "separate skus with different barcodes",
    "related barcodes",
)


def is_internal_warning(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in _INTERNAL_WARNING_MARKERS)


def warnings_for_ui(warnings: list[str] | None) -> list[str]:
    """Return deduplicated shopper-facing warnings, dropping internal ops copy."""
    seen: set[str] = set()
    out: list[str] = []
    for item in warnings or []:
        text = str(item).strip()
        if not text or is_internal_warning(text):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out[:5]


def catalog_conflict_user_warnings(technical_reason: str) -> list[str]:
    """Translate a catalog conflict reason into plain-language shopper guidance."""
    lower = (technical_reason or "").lower()
    messages = [
        "We found conflicting product information for this barcode in our Hong Kong catalog.",
    ]

    if any(token in lower for token in ("recipe", "flavor", "flavour", "variant")):
        messages.append(
            "Different flavors or varieties may look similar online — "
            "check the flavor on the packaging before you buy."
        )
    elif "pack-size" in lower or re.search(r"\b\d+\s*kg\b", lower):
        messages.append(
            "This product may be sold in more than one pack size — "
            "confirm the weight on the packaging before you buy."
        )
    elif "animal" in lower:
        messages.append(
            "Listings disagree on whether this product is for cats or dogs — "
            "check the label before you buy."
        )
    else:
        messages.append(
            "Please double-check the product name and details on the retailer's page before buying."
        )

    return messages


def unverified_barcode_user_warnings() -> list[str]:
    return [
        "We could not fully verify this barcode against product listings.",
        "Please check the product details carefully before buying.",
    ]


def extraction_failed_user_warnings() -> list[str]:
    return [
        "We could not reliably identify this product from available sources.",
        "Please try again later or confirm the details on the retailer's website.",
    ]
