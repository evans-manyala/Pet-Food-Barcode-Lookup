"""
models.py – Shared Pydantic data models.
"""

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class NutritionalInfo(BaseModel):
    """Guaranteed / nutritional analysis values for a pet food product."""
    crude_protein_min: Optional[str] = None
    crude_fat_min: Optional[str] = None
    crude_fiber_max: Optional[str] = None
    moisture_max: Optional[str] = None
    ash_max: Optional[str] = None
    calories: Optional[str] = None
    other: dict[str, str] = Field(default_factory=dict)


class RetailerListing(BaseModel):
    """A single Hong Kong retailer selling the product."""
    retailer_name: str
    url: str
    price_hkd: str          # e.g. "HK$189.00"
    in_stock: Optional[bool] = None
    notes: Optional[str] = None


class ProductInfo(BaseModel):
    """Full product record stored in Redis and Pinecone."""
    barcode: str
    product_name: str
    brand: Optional[str] = None
    target_animal: Optional[str] = None       # "Dog", "Cat", "Dog & Cat"
    manufacturer_url: Optional[str] = None
    image_url: Optional[str] = None
    nutritional_info: Optional[NutritionalInfo] = None
    hk_retailers: list[RetailerListing] = Field(default_factory=list)

    # ── Verification metadata (added in v2) ───────────────────────────────
    barcode_verified: bool = False
    identity_confidence: str = "low"          # "high" | "medium" | "low"
    evidence_urls: list[str] = Field(default_factory=list)
    barcode_evidence: Optional[str] = None    # short snippet linking barcode to product
    warnings: list[str] = Field(default_factory=list)

    # ── Debug ─────────────────────────────────────────────────────────────
    raw_llm_response: Optional[str] = None
    