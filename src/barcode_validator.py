"""
barcode_validator.py – Validates common 1-D barcodes used on retail products.

Supported formats
-----------------
  EAN-13  (13 digits)  – most common globally
  UPC-A   (12 digits)  – North-America standard
  EAN-8   ( 8 digits)  – short-space packaging
"""

from __future__ import annotations
import re
from dataclasses import dataclass
from enum import Enum


class BarcodeFormat(str, Enum):
    EAN13 = "EAN-13"
    UPCA  = "UPC-A"
    EAN8  = "EAN-8"
    UNKNOWN = "Unknown"


@dataclass
class BarcodeValidationResult:
    is_valid: bool
    barcode: str
    fmt: BarcodeFormat
    error: str = ""

    def __str__(self) -> str:
        if self.is_valid:
            return f"✔  {self.barcode}  [{self.fmt}]"
        return f"✘  {self.barcode}  – {self.error}"


# ─── Internal helpers ────────────────────────────────────────────────────────

def _gs1_checksum_valid(digits: str) -> bool:
    """
    GS-1 check digit algorithm used by EAN-13, UPC-A, and EAN-8.

    For a string of N digits the check digit is the last one.
    Weights alternate 1 and 3 starting from the *left* for EAN-13/UPC-A
    and identically for EAN-8.
    """
    weights = [1 if i % 2 == 0 else 3 for i in range(len(digits) - 1)]
    total = sum(int(d) * w for d, w in zip(digits[:-1], weights))
    check = (10 - (total % 10)) % 10
    return check == int(digits[-1])


def _detect_format(digits: str) -> BarcodeFormat:
    if len(digits) == 13:
        return BarcodeFormat.EAN13
    if len(digits) == 12:
        return BarcodeFormat.UPCA
    if len(digits) == 8:
        return BarcodeFormat.EAN8
    return BarcodeFormat.UNKNOWN


# ─── Public API ──────────────────────────────────────────────────────────────

def validate_barcode(raw: str) -> BarcodeValidationResult:
    """
    Validate a barcode string.

    Parameters
    ----------
    raw : str
        The raw barcode string typed or scanned by the user.
        Spaces and hyphens are stripped automatically.

    Returns
    -------
    BarcodeValidationResult
    """
    cleaned = re.sub(r"[\s\-]", "", raw.strip())

    if not cleaned:
        return BarcodeValidationResult(
            is_valid=False, barcode=raw,
            fmt=BarcodeFormat.UNKNOWN,
            error="Barcode is empty."
        )

    if not cleaned.isdigit():
        return BarcodeValidationResult(
            is_valid=False, barcode=cleaned,
            fmt=BarcodeFormat.UNKNOWN,
            error=f"Barcode contains non-digit characters: '{cleaned}'"
        )

    fmt = _detect_format(cleaned)
    if fmt is BarcodeFormat.UNKNOWN:
        return BarcodeValidationResult(
            is_valid=False, barcode=cleaned,
            fmt=fmt,
            error=(
                f"Unsupported barcode length ({len(cleaned)} digits). "
                "Expected 8 (EAN-8), 12 (UPC-A), or 13 (EAN-13)."
            )
        )

    if not _gs1_checksum_valid(cleaned):
        return BarcodeValidationResult(
            is_valid=False, barcode=cleaned,
            fmt=fmt,
            error="Invalid check digit – barcode may be mis-typed or corrupted."
        )

    return BarcodeValidationResult(is_valid=True, barcode=cleaned, fmt=fmt)
