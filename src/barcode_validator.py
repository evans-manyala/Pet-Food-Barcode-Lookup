"""
barcode_validator.py – Validates common 1-D barcodes used on retail products.

Supported formats
-----------------
  EAN-13 / GTIN-13  (13 digits)
  UPC-A  / GTIN-12  (12 digits)
  EAN-8  / GTIN-8   (8 digits)

Also supports common UPC-A display issue:
  11 digits where the leading UPC number-system digit 0 was omitted.
  Example: 52742068435 -> normalized to 052742068435
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class BarcodeFormat(str, Enum):
    EAN13 = "EAN-13"
    UPCA = "UPC-A"
    EAN8 = "EAN-8"
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


def _detect_format(digits: str) -> BarcodeFormat:
    if len(digits) == 13:
        return BarcodeFormat.EAN13
    if len(digits) == 12:
        return BarcodeFormat.UPCA
    if len(digits) == 8:
        return BarcodeFormat.EAN8
    return BarcodeFormat.UNKNOWN


def _gs1_check_digit(data_digits: str) -> int:
    """
    Correct GS1/GTIN check digit calculation for GTIN-8, GTIN-12/UPC-A,
    and GTIN-13/EAN-13.

    Exclude the final check digit, start from the rightmost data digit, and
    apply weights 3,1,3,1... moving left.
    """
    total = 0
    for position_from_right, ch in enumerate(reversed(data_digits), start=1):
        weight = 3 if position_from_right % 2 == 1 else 1
        total += int(ch) * weight
    return (10 - (total % 10)) % 10


def _gs1_checksum_valid(digits: str) -> bool:
    if len(digits) < 2 or not digits.isdigit():
        return False
    return _gs1_check_digit(digits[:-1]) == int(digits[-1])


def _normalise_candidates(cleaned: str) -> list[str]:
    candidates = [cleaned]
    if len(cleaned) == 11:
        candidates.append("0" + cleaned)
    return list(dict.fromkeys(candidates))


def validate_barcode(raw: str) -> BarcodeValidationResult:
    cleaned = re.sub(r"[\s\-]", "", str(raw or "").strip())

    if not cleaned:
        return BarcodeValidationResult(False, str(raw), BarcodeFormat.UNKNOWN, "Barcode is empty.")

    if not cleaned.isdigit():
        return BarcodeValidationResult(
            False,
            cleaned,
            BarcodeFormat.UNKNOWN,
            f"Barcode contains non-digit characters: '{cleaned}'",
        )

    for candidate in _normalise_candidates(cleaned):
        fmt = _detect_format(candidate)
        if fmt is not BarcodeFormat.UNKNOWN and _gs1_checksum_valid(candidate):
            return BarcodeValidationResult(True, candidate, fmt)

    fmt = _detect_format(cleaned)
    if fmt is BarcodeFormat.UNKNOWN:
        if len(cleaned) == 11:
            return BarcodeValidationResult(
                False,
                cleaned,
                BarcodeFormat.UNKNOWN,
                "Unsupported 11-digit barcode. Tried normalizing as UPC-A with leading zero "
                f"0{cleaned}, but the check digit did not pass.",
            )
        return BarcodeValidationResult(
            False,
            cleaned,
            BarcodeFormat.UNKNOWN,
            f"Unsupported barcode length ({len(cleaned)} digits). Expected 8 (EAN-8), "
            "12 (UPC-A), 13 (EAN-13), or 11 digits when a UPC-A leading zero was omitted.",
        )

    return BarcodeValidationResult(False, cleaned, fmt, "Invalid check digit – barcode may be mis-typed or corrupted.")
