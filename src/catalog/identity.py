"""Catalog-first product identity, conflict detection, and retailer brand filtering."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from .models import CatalogListing
from .normalize import identity_tokens, normalize_match_text
from src.user_warnings import catalog_conflict_user_warnings

from .trust import is_trusted_catalog_barcode_listing

log = logging.getLogger(__name__)

IdentityStatus = Literal["resolved", "conflict", "insufficient"]

_SCRAPE_PREFERRED = frozenset({"petsorder", "legopet", "petmarket", "wnp"})

# Equivalent brand labels seen across HK retailers (normalized keys).
_BRAND_EQUIVALENTS: dict[str, frozenset[str]] = {
    "kong style": frozenset({"kong style", "港風味", "犬風味", "kongs style"}),
    "almo nature": frozenset({"almo nature", "almo"}),
    "whiskas": frozenset({"whiskas", "偉嘉"}),
    "hill's": frozenset({"hill's", "hills", "hill s"}),
}

# Strong competing brands — if listing lead brand differs from verified brand, reject.
_KNOWN_BRAND_TOKENS = frozenset({
    "whiskas", "royal", "canin", "hills", "orijen", "acana", "purina", "fussie",
    "weruva", "naturea", "kong", "style", "almo", "nature", "canagan", "ziwi",
    "kakato", "inaba", "ciao", "wellness", "nutrience", "solid", "gold",
})

# Recipe / size variant cues used to detect multi-SKU barcode collisions.
_RECIPE_FLAVOR_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("duck", re.compile(r"duck|鴨肉|鴨", re.I)),
    ("chicken", re.compile(r"chicken|雞肉|雞", re.I)),
    ("salmon", re.compile(r"salmon|三文魚", re.I)),
    ("beef", re.compile(r"beef|牛肉", re.I)),
    ("tuna", re.compile(r"tuna|吞拿|鮪", re.I)),
    ("rabbit", re.compile(r"rabbit|兔肉", re.I)),
)

_WEIGHT_VARIANT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("1.5kg", re.compile(r"\b1\.5\s*kg\b|1\.5公斤|1\.5千克|1-5kg|1\.5kg", re.I)),
    ("2kg", re.compile(r"\b2\s*kg\b|2公斤|2千克", re.I)),
    ("3.5kg", re.compile(r"\b3\.5\s*kg\b|3\.5公斤|3\.5千克|3-5kg|3\.5kg", re.I)),
    ("4kg", re.compile(r"\b4\s*kg\b|4公斤|4千克", re.I)),
    ("10kg", re.compile(r"\b10\s*kg\b|10公斤|10千克", re.I)),
)

# Royal Canin / brand line codes that share a barcode family but are different SKUs.
_PRODUCT_LINE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("in27", re.compile(r"\bin\s*27\b|indoor\s*27|室內\s*27|#in27|\bin27\b", re.I)),
    ("in7", re.compile(r"\bin\s*7\+?\b|indoor\s*7\+?|indoor\s*7\b|室內\s*7\+?|#in7|\bin7\b", re.I)),
    ("in_spayed", re.compile(r"spayed|已絕育|sterilis", re.I)),
)

_LINE_CODE_CONFLICTS: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (frozenset({"in27"}), frozenset({"in7"})),
)


@dataclass
class CatalogIdentityResult:
    status: IdentityStatus
    data: dict | None = None
    facts: str = ""
    trusted_hits: list[CatalogListing] = field(default_factory=list)
    reason: str = ""


def brands_compatible(verified_brand: str | None, listing_brand: str | None) -> bool:
    """True when listing brand matches verified brand (including known aliases)."""
    a = normalize_match_text(verified_brand)
    b = normalize_match_text(listing_brand)
    if not a or not b:
        return True
    if a == b or a in b or b in a:
        return True
    for _key, aliases in _BRAND_EQUIVALENTS.items():
        if a in aliases and b in aliases:
            return True
        if a in aliases and any(alias in b for alias in aliases):
            return True
        if b in aliases and any(alias in a for alias in aliases):
            return True
    return False


def infer_target_animal(listing: CatalogListing) -> str | None:
    """Infer Dog / Cat / Dog & Cat from listing text and category."""
    text = normalize_match_text(
        " ".join(filter(None, [listing.title_en, listing.title_zh, listing.category, listing.description]))
    )
    cat = bool(re.search(r"\bcat\b|貓貓|貓用|成貓|幼貓|全貓", text))
    dog = bool(re.search(r"\bdog\b|狗狗|犬|成犬|幼犬|全犬", text))
    if cat and dog:
        return "Dog & Cat"
    if cat:
        return "Cat"
    if dog:
        return "Dog"
    return None


def _listing_brand_key(listing: CatalogListing) -> str:
    brand = normalize_match_text(listing.brand)
    if brand:
        for canonical, aliases in _BRAND_EQUIVALENTS.items():
            if brand in aliases:
                return canonical
        return brand
    tokens = identity_tokens(listing.title_en, listing.brand)
    if tokens:
        tok = tokens[0]
        for canonical, aliases in _BRAND_EQUIVALENTS.items():
            if tok in aliases:
                return canonical
        return tok
    return normalize_match_text(listing.title_en)[:40]


def _identity_signature(listing: CatalogListing) -> tuple[str, str | None]:
    return _listing_brand_key(listing), infer_target_animal(listing)


def trusted_barcode_hits(barcode: str, listings: list[CatalogListing]) -> list[CatalogListing]:
    return [
        item for item in listings
        if is_trusted_catalog_barcode_listing(item, barcode)
    ]


def extract_recipe_flavors(text: str) -> set[str]:
    """Return normalized recipe flavor tokens found in product text or URL."""
    if not text:
        return set()
    return {
        label
        for label, pattern in _RECIPE_FLAVOR_RULES
        if pattern.search(text)
    }


def extract_weight_variants(text: str) -> set[str]:
    if not text:
        return set()
    return {
        label
        for label, pattern in _WEIGHT_VARIANT_RULES
        if pattern.search(text)
    }


def extract_product_line_codes(text: str) -> set[str]:
    """Return normalized product-line tokens (e.g. Royal Canin IN27 vs IN7+)."""
    if not text:
        return set()
    return {
        label
        for label, pattern in _PRODUCT_LINE_RULES
        if pattern.search(text)
    }


def product_line_codes_compatible(verified: set[str], listing: set[str]) -> bool:
    if not verified or not listing:
        return True
    for left, right in _LINE_CODE_CONFLICTS:
        if (verified & left and listing & right) or (verified & right and listing & left):
            return False
    return True


def recipe_flavors_compatible(verified: set[str], listing: set[str]) -> bool:
    if not verified or not listing:
        return True
    return bool(verified & listing)


def listing_text_blob(listing: CatalogListing) -> str:
    return " ".join(filter(None, [
        listing.title_en,
        listing.title_zh,
        listing.title_norm,
        listing.product_url,
        listing.description,
    ]))


def product_identity_text(
    product_name: str | None,
    brand: str | None,
    extra: str = "",
) -> str:
    return " ".join(filter(None, [brand or "", product_name or "", extra]))


def listing_matches_product_variant(
    listing: CatalogListing | str,
    product_name: str | None,
    brand: str | None,
) -> bool:
    """Reject retailer rows whose recipe flavor or weight variant conflicts."""
    if isinstance(listing, str):
        blob = listing
    else:
        blob = listing_text_blob(listing)

    identity = product_identity_text(product_name, brand)
    verified_flavors = extract_recipe_flavors(identity)
    listing_flavors = extract_recipe_flavors(blob)
    if not recipe_flavors_compatible(verified_flavors, listing_flavors):
        return False

    verified_weights = extract_weight_variants(identity)
    listing_weights = extract_weight_variants(blob)
    if verified_weights and listing_weights and not (verified_weights & listing_weights):
        return False

    verified_lines = extract_product_line_codes(identity)
    listing_lines = extract_product_line_codes(blob)
    if not product_line_codes_compatible(verified_lines, listing_lines):
        return False
    if verified_lines and listing_lines and not (verified_lines & listing_lines):
        return False

    return True


def detect_recipe_variant_conflict(trusted: list[CatalogListing]) -> str | None:
    """Multiple trusted rows for one barcode with different recipes or pack sizes."""
    if len(trusted) < 2:
        return None

    flavor_sets = [extract_recipe_flavors(listing_text_blob(item)) for item in trusted]
    flavor_sets = [fs for fs in flavor_sets if fs]
    if len(flavor_sets) >= 2 and len({frozenset(fs) for fs in flavor_sets}) > 1:
        labels = []
        for item in trusted:
            flavors = extract_recipe_flavors(listing_text_blob(item))
            if flavors:
                sample = item.title_en or item.title_zh or item.product_url
                labels.append(f"{'/'.join(sorted(flavors))} ({sample[:45]})")
        if labels:
            return (
                "Multiple recipe variants share this barcode: "
                + "; ".join(labels[:4])
            )

    weight_sets = [extract_weight_variants(listing_text_blob(item)) for item in trusted]
    weight_sets = [ws for ws in weight_sets if ws]
    if len(weight_sets) >= 2 and len({frozenset(ws) for ws in weight_sets}) > 1:
        return "Multiple pack-size variants share this barcode."

    return None


def detect_trusted_conflict(trusted: list[CatalogListing]) -> str | None:
    """
    Return a conflict reason when trusted listings disagree on brand or target animal.
    """
    if len(trusted) < 2:
        return None

    signatures: dict[tuple[str, str | None], list[CatalogListing]] = {}
    for item in trusted:
        sig = _identity_signature(item)
        signatures.setdefault(sig, []).append(item)

    brand_keys = {_listing_brand_key(item) for item in trusted}
    animals = {infer_target_animal(item) for item in trusted if infer_target_animal(item)}

    if len(signatures) > 1:
        parts = []
        for (brand_key, animal), group in signatures.items():
            sample = group[0].title_en or group[0].title_zh or group[0].product_url
            parts.append(f"{brand_key}/{animal or '?'} ({sample[:50]})")
        return "Trusted catalog sources disagree: " + "; vs ".join(parts[:4])

    if len(brand_keys) > 1:
        return f"Trusted catalog sources disagree on brand: {', '.join(sorted(brand_keys)[:4])}"

    if len(animals) > 1:
        return f"Trusted catalog sources disagree on target animal: {', '.join(sorted(animals))}"

    recipe_conflict = detect_recipe_variant_conflict(trusted)
    if recipe_conflict:
        return recipe_conflict

    return None


def _pick_canonical_listing(listings: list[CatalogListing]) -> CatalogListing:
    def score(item: CatalogListing) -> tuple:
        return (
            item.source in _SCRAPE_PREFERRED,
            bool(item.description),
            bool(item.image_url),
            len(item.title_en or item.title_zh or ""),
        )

    return max(listings, key=score)


def _parse_nutrition_from_description(description: str) -> dict:
    """Best-effort guaranteed analysis from catalog description text."""
    if not description:
        return {}
    text = description.replace("\n", " ")
    out: dict[str, str] = {}

    patterns = [
        (r"(?:crude\s*)?protein[^0-9]{0,20}(\d+(?:\.\d+)?)\s*%", "crude_protein_min"),
        (r"(?:crude\s*)?fat[^0-9]{0,20}(\d+(?:\.\d+)?)\s*%", "crude_fat_min"),
        (r"(?:crude\s*)?(?:fiber|fibre)[^0-9]{0,20}(\d+(?:\.\d+)?)\s*%", "crude_fiber_max"),
        (r"moisture[^0-9]{0,20}(\d+(?:\.\d+)?)\s*%", "moisture_max"),
        (r"(?:crude\s*)?ash[^0-9]{0,20}(\d+(?:\.\d+)?)\s*%", "ash_max"),
        (r"(\d+(?:\.\d+)?)\s*kcal\s*/\s*100\s*g", "calories"),
        (r"(\d+(?:\.\d+)?)\s*kcal\s*/\s*kg", "calories"),
    ]
    for pattern, key in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match and key not in out:
            val = match.group(1)
            out[key] = f"{val} kcal/100g" if key == "calories" and "100" in pattern else val
    return out


def build_catalog_identity_dict(
    barcode: str,
    trusted: list[CatalogListing],
    *,
    confidence: str,
    source_label: str,
) -> dict:
    canonical = _pick_canonical_listing(trusted)
    product_name = (canonical.title_en or canonical.title_zh or "Unknown Product").strip()
    brand = (canonical.brand or "").strip() or None
    target_animal = infer_target_animal(canonical)
    evidence_urls = list(dict.fromkeys(item.product_url for item in trusted if item.product_url))
    nutrition = _parse_nutrition_from_description(canonical.description or "")

    return {
        "product_name": product_name,
        "brand": brand,
        "target_animal": target_animal,
        "manufacturer_url": None,
        "nutritional_info": nutrition,
        "barcode_verified": True,
        "identity_confidence": confidence,
        "evidence_urls": evidence_urls,
        "barcode_evidence": (
            f"{source_label}: {len(trusted)} trusted local listing(s) agree on "
            f"{brand or 'product'} ({target_animal or 'unknown animal'}) for barcode {barcode}."
        ),
        "warnings": [],
    }


def format_catalog_identity_facts(barcode: str, trusted: list[CatalogListing]) -> str:
    lines = [
        f"Local HK catalog identity resolution for barcode {barcode}.",
        f"Trusted listings: {len(trusted)}.",
    ]
    for item in trusted[:8]:
        lines.append(
            " | ".join([
                f"source={item.source}",
                f"brand={item.brand or '—'}",
                f"title={item.title_en or item.title_zh or '—'}",
                f"animal={infer_target_animal(item) or '?'}",
                f"url={item.product_url}",
            ])
        )
    return "\n".join(lines)


def resolve_catalog_identity(
    barcode: str,
    all_hits: list[CatalogListing],
    *,
    min_sources_high: int = 2,
) -> CatalogIdentityResult:
    """
    Resolve product identity from trusted catalog hits.

    - 2+ agreeing trusted hits → high confidence, skip live identity search
    - 1 trusted hit → medium confidence
    - conflicting trusted hits → conflict (do not cache)
    - 0 trusted hits → insufficient (fall back to live search)
    """
    trusted = trusted_barcode_hits(barcode, all_hits)
    facts = format_catalog_identity_facts(barcode, trusted)

    if not trusted:
        return CatalogIdentityResult(
            status="insufficient",
            facts=facts,
            trusted_hits=trusted,
            reason="no trusted catalog hits",
        )

    conflict = detect_trusted_conflict(trusted)
    if conflict:
        log.warning("Catalog identity conflict for %s: %s", barcode, conflict)
        return CatalogIdentityResult(
            status="conflict",
            facts=facts,
            trusted_hits=trusted,
            reason=conflict,
            data={
                "product_name": None,
                "brand": None,
                "target_animal": None,
                "barcode_verified": False,
                "identity_confidence": "low",
                "evidence_urls": list(dict.fromkeys(h.product_url for h in trusted)),
                "barcode_evidence": conflict,
                "warnings": catalog_conflict_user_warnings(conflict),
            },
        )

    confidence = "high" if len(trusted) >= min_sources_high else "medium"
    data = build_catalog_identity_dict(
        barcode,
        trusted,
        confidence=confidence,
        source_label="Local HK catalog",
    )
    log.info(
        "Catalog-first identity for %s (%s, %d trusted hit(s))",
        barcode,
        confidence,
        len(trusted),
    )
    return CatalogIdentityResult(
        status="resolved",
        data=data,
        facts=facts,
        trusted_hits=trusted,
    )


def _lead_brand_token(text: str) -> str | None:
    norm = normalize_match_text(text)
    if not norm:
        return None
    lead = norm.split("|")[0].split("-")[0].strip()
    for tok in re.findall(r"[a-z]{3,}", lead):
        if tok in _KNOWN_BRAND_TOKENS:
            return tok
    return None


def listing_matches_verified_product(
    listing: CatalogListing,
    brand: str | None,
    product_name: str | None,
    target_animal: str | None = None,
) -> bool:
    """
    Drop catalog retailer rows whose brand or animal conflicts with verified identity.
    """
    verified_brand = normalize_match_text(brand)
    listing_brand = normalize_match_text(listing.brand)
    title = " ".join(filter(None, [listing.title_en, listing.title_zh, listing.title_norm]))

    if verified_brand and listing_brand and not brands_compatible(brand, listing.brand):
        return False

    if verified_brand:
        title_norm = normalize_match_text(title)
        if verified_brand not in title_norm and not brands_compatible(brand, listing.brand):
            lead = _lead_brand_token(title)
            if lead and not brands_compatible(brand, lead):
                return False

    listing_animal = infer_target_animal(listing)
    if listing_animal and target_animal:
        la = listing_animal.lower().replace(" ", "")
        va = target_animal.lower().replace(" ", "")
        if "dog" in la and "cat" in va and "dog" not in va:
            return False
        if "cat" in la and "dog" in va and "cat" not in va:
            return False
        if la in {"dog", "cat"} and va in {"dog", "cat"} and la != va:
            return False

    if verified_brand and not listing_brand:
        tokens = identity_tokens(product_name, brand)
        plain = normalize_match_text(title)
        if tokens:
            hits = sum(1 for tok in tokens[:6] if tok in plain)
            if hits == 0 and _lead_brand_token(title):
                return False

    if not listing_matches_product_variant(listing, product_name, brand):
        return False

    return True


def catalog_conflicts_with_verified_identity(
    barcode: str,
    trusted: list[CatalogListing],
    brand: str | None,
    product_name: str | None,
    target_animal: str | None,
) -> str | None:
    """
    After live search verifies identity, reject if trusted catalog unanimously disagrees
    on brand or target animal.

    Intentionally limited to brand and cross-species animal conflicts only.
    Product line codes, pack sizes, and recipe variants are retailer-level concerns
    (Phase 4) — applying them here would cause false downgrades when catalog rows
    carry slightly different variant names for the same barcode.
    """
    if not trusted:
        return None

    conflict = detect_trusted_conflict(trusted)
    if conflict:
        return conflict

    canonical = _pick_canonical_listing(trusted)
    cat_brand = canonical.brand or _listing_brand_key(canonical)
    cat_animal = infer_target_animal(canonical)

    if brand and cat_brand and not brands_compatible(brand, cat_brand):
        return (
            f"Live search identity ({brand}) conflicts with trusted catalog "
            f"({cat_brand}) for barcode {barcode}."
        )

    # Only fire on a clear cross-species conflict (Dog ↔ Cat).
    # Same-species variants (size, line code, flavour) are not identity-level conflicts.
    if target_animal and cat_animal:
        ta = normalize_match_text(target_animal)
        ca = normalize_match_text(cat_animal)
        if ta != ca:
            ta_has_dog = "dog" in ta
            ta_has_cat = "cat" in ta
            ca_has_dog = "dog" in ca
            ca_has_cat = "cat" in ca
            if (ta_has_dog and not ca_has_dog and ca_has_cat) or (
                ta_has_cat and not ca_has_cat and ca_has_dog
            ):
                return (
                    f"Live search target animal ({target_animal}) conflicts with "
                    f"trusted catalog ({cat_animal}) for barcode {barcode}."
                )

    return None
