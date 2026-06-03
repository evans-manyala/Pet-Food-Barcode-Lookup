"""
llm_searcher.py – Gemini 2.5 Flash via Vertex AI + ADC + Google Search Grounding.

Root cause of missing URLs (previous version)
----------------------------------------------
Gemini Search Grounding returns two separate things:
  A) response TEXT  – a prose summary of what was found
  B) grounding_metadata.grounding_chunks  – the ACTUAL URLs Google searched

The previous code asked Gemini to *write* URLs inside the text.
Gemini often skips this or writes store names without URLs.
The extraction step then found no URLs in the text → set them to null → 
URL validator dropped everything → empty retailer list.

Correct approach (this version)
---------------------------------
Step 1  GROUNDED SEARCH  – call Gemini with search tool.
        After the call, pull real source URLs from grounding_metadata directly.
        Filter to known HK pet retailers. These are URLs Google actually found.

Step 2  PRICE EXTRACTION  – a second call (no search tool) takes the grounded
        TEXT and the real HK URLs and asks Gemini to match price/stock/name to
        each URL. No URL invention possible because URLs are pre-supplied.

Step 3  URL VALIDATION  – HEAD-check each URL before returning.
        Retry up to 2 rounds if fewer than 2 pass.
"""

from __future__ import annotations
import json
import re
import time
import logging
from typing import Optional
from urllib.parse import urlparse

import requests as http_requests
from google import genai
from google.genai import types

from .config import get_settings
from .models import ProductInfo, NutritionalInfo, RetailerListing

log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

_URL_TIMEOUT         = 10
_MIN_RETAILERS       = 2
_MAX_RETAILERS       = 5
_EXTRA_SEARCH_ROUNDS = 2

# Known HK pet retailer domains — used to filter grounding chunk URLs
_HK_RETAILER_DOMAINS: list[str] = [
    "hktvmall.com",
    "petstation.com.hk",
    "petsworld.com.hk",
    "pawsmore.com.hk",
    "petboo.com.hk",
    "ipetdog.com",
    "mrpetshk.com",
    "petfashion.com.hk",
    "petscorner.com.hk",
    "petshop168.com",
    "pet-city.com.hk",
    "goopets.com",
    "petoo.com.hk",
    "pet-club.com.hk",
    "vetopia.com.hk",
    "wnp.com.hk",
    "epet.com.hk",
    "petmarket.com.hk",
    "petmall.com.hk",
    "dogcatworld.com.hk",
]

# ─── Grounding metadata helpers ───────────────────────────────────────────────

def _collect_text(response) -> str:
    """Collect all text parts from a google-genai response."""
    chunks: list[str] = []
    # Fast path
    try:
        t = response.text
        if t:
            return t
    except Exception:
        pass
    # Fallback: iterate candidates
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        if not content:
            continue
        for part in getattr(content, "parts", []) or []:
            t = getattr(part, "text", None)
            if t:
                chunks.append(t)
    return "\n".join(chunks).strip()


def _extract_grounding_urls(response) -> list[str]:
    """
    Pull source URLs from Gemini's grounding_metadata.grounding_chunks.
    These are the REAL URLs that Google Search returned — far more reliable
    than asking the model to write URLs in its text response.
    """
    seen: set[str] = set()
    urls: list[str] = []
    for candidate in getattr(response, "candidates", []) or []:
        meta = getattr(candidate, "grounding_metadata", None)
        if not meta:
            continue
        for chunk in getattr(meta, "grounding_chunks", []) or []:
            web = getattr(chunk, "web", None)
            uri = getattr(web, "uri", None) if web else None
            if uri and uri not in seen:
                seen.add(uri)
                urls.append(uri)
    return urls


def _filter_hk_retailers(urls: list[str]) -> list[str]:
    """Keep only URLs whose domain matches a known HK pet retailer."""
    hk_urls: list[str] = []
    for url in urls:
        try:
            host = urlparse(url).netloc.lower().lstrip("www.")
            if any(host == d or host.endswith("." + d) for d in _HK_RETAILER_DOMAINS):
                hk_urls.append(url)
        except Exception:
            continue
    return hk_urls


def _grounding_search_queries(response) -> list[str]:
    queries: list[str] = []
    for candidate in getattr(response, "candidates", []) or []:
        meta = getattr(candidate, "grounding_metadata", None)
        if meta and getattr(meta, "web_search_queries", None):
            queries.extend(meta.web_search_queries)
    return queries


# ─── JSON extraction ──────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
    start = text.find("{")
    if start < 0:
        raise ValueError(f"No JSON object in model response:\n{text[:600]}")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError(f"JSON was not an object:\n{text[:600]}")
    return obj


# ─── URL validator ────────────────────────────────────────────────────────────

def _url_is_reachable(url: str) -> bool:
    """
    HEAD-check a URL. Returns True for 2xx/3xx responses where the final URL
    is not a homepage (path length > 3 characters after last slash).
    """
    try:
        resp = http_requests.head(
            url,
            allow_redirects=True,
            timeout=_URL_TIMEOUT,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        if resp.status_code >= 400:
            log.debug("  URL %s → HTTP %s", url, resp.status_code)
            return False
        # Check if we were redirected to a homepage
        final_path = urlparse(resp.url).path.rstrip("/")
        if len(final_path) < 4:
            log.debug("  URL redirects to homepage: %s → %s", url, resp.url)
            return False
        return True
    except Exception as exc:
        log.debug("  URL check error (%s): %s", type(exc).__name__, url)
        return False


# ─── Prompts ─────────────────────────────────────────────────────────────────

_PRODUCT_SEARCH_PROMPT = """\
Search the internet for the pet food product with barcode {barcode}.
This is a DOG or CAT food product.

Find ALL of the following, preferably from the manufacturer's official website:
- Full product name (including flavour, variety, and size/weight)
- Brand name
- Target animal (Dog, Cat, or both)
- Direct URL to the product page on the manufacturer's official website
- Direct URL to a product image (jpg/png/webp)
- Guaranteed / nutritional analysis:
    Crude Protein (min), Crude Fat (min), Crude Fiber (max),
    Moisture (max), Ash (max), Calories, and any other nutrients on the label

Be specific and factual. Include exact percentage values.
"""

_PRODUCT_EXTRACT_PROMPT = """\
Below are research notes about a pet food product found via Google Search.
Extract the information and return ONLY a valid JSON object.
No prose, no markdown, no explanation — just the JSON.

Research notes:
{facts}

JSON structure:
{{
  "product_name": "<full name>",
  "brand": "<brand>",
  "target_animal": "<Dog | Cat | Dog & Cat>",
  "manufacturer_url": "<URL or null>",
  "image_url": "<direct image URL or null>",
  "nutritional_info": {{
    "crude_protein_min": "<e.g. 30% or null>",
    "crude_fat_min": "<value or null>",
    "crude_fiber_max": "<value or null>",
    "moisture_max": "<value or null>",
    "ash_max": "<value or null>",
    "calories": "<value or null>",
    "other": {{ "<nutrient>": "<value>" }}
  }}
}}
"""

_RETAILER_SEARCH_PROMPT = """\
Search Hong Kong online pet food retailers for this product and report PRICES:

  Product : {product_name}
  Brand   : {brand}
  Barcode : {barcode}

Search specifically on these HK retailer websites:
  hktvmall.com, petstation.com.hk, petboo.com.hk, pawsmore.com.hk,
  pet-city.com.hk, goopets.com, ipetdog.com, mrpetshk.com,
  petscorner.com.hk, petsworld.com.hk, vetopia.com.hk

For EACH retailer that sells this product, tell me:
1. Store name
2. Price in Hong Kong Dollars (HK$)
3. Whether it is in stock
4. Any notes (pack size, promotion, free delivery threshold)

Important: Focus on reporting store names and prices accurately.
"""

_PRICE_MATCH_PROMPT = """\
Below are:
  A) Research notes with prices from Hong Kong pet food retailers
  B) Real product page URLs from those retailers (confirmed by Google Search)

Match each URL to its retailer name and price from the notes.
Return ONLY a valid JSON object — no prose, no markdown.

Research notes (prices/names):
{facts}

Real product page URLs found by Google Search:
{url_list}

Rules:
- Use the URLs EXACTLY as given — do not modify or construct new ones.
- Match each URL to a retailer name and price from the notes.
- If a URL cannot be matched to a price, still include it with price_hkd set to null.
- in_stock: true if notes say "in stock" or show a price, false if "out of stock", null if unknown.

JSON structure:
{{
  "hk_retailers": [
    {{
      "retailer_name": "<store name>",
      "url": "<exact URL as provided>",
      "price_hkd": "<e.g. HK$189.00 or null>",
      "in_stock": <true | false | null>,
      "notes": "<pack size, promotion, etc.>"
    }}
  ]
}}
"""


# ─── Searcher class ───────────────────────────────────────────────────────────

class ProductSearcher:
    """Gemini 2.5 Flash via Vertex AI + ADC. URLs come from grounding metadata."""

    def __init__(self) -> None:
        cfg = get_settings()

        if not cfg.google_cloud_project:
            raise EnvironmentError(
                "GOOGLE_CLOUD_PROJECT is not set in .env.\n"
                "Run:\n"
                "  gcloud auth application-default login\n"
                "  gcloud auth application-default set-quota-project YOUR_PROJECT_ID"
            )

        self._client = genai.Client(
            vertexai=True,
            project=cfg.google_cloud_project,
            location=cfg.google_cloud_location,
        )
        self._model_name = cfg.gemini_model

        # Config WITH search grounding (Step 1 only)
        self._search_config = types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=4096,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        )
        # Config WITHOUT search tool (Steps 2 + price matching)
        self._extract_config = types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=2048,
        )

        log.info(
            "Vertex AI ready | project=%s location=%s model=%s search_grounding=ON",
            cfg.google_cloud_project, cfg.google_cloud_location, self._model_name,
        )

    # ── Step 1: grounded search ───────────────────────────────────────────────

    def _grounded_search(self, prompt: str) -> tuple[str, list[str]]:
        """
        Call Gemini with the Google Search tool.
        Returns (response_text, all_source_urls_from_grounding_metadata).
        """
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=self._search_config,
        )
        text = _collect_text(response)
        all_urls = _extract_grounding_urls(response)
        queries  = _grounding_search_queries(response)

        log.info(
            "Grounded search: %d chars text | %d source URLs | queries=%s",
            len(text), len(all_urls), queries[:4],
        )
        log.debug("Grounding source URLs:\n%s", "\n".join(all_urls))
        return text, all_urls

    # ── Step 2: extraction ────────────────────────────────────────────────────

    def _extract_product_json(self, facts: str) -> dict:
        prompt   = _PRODUCT_EXTRACT_PROMPT.format(facts=facts)
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=self._extract_config,
        )
        return _extract_json(_collect_text(response))

    def _match_prices_to_urls(
        self, facts: str, hk_urls: list[str]
    ) -> list[dict]:
        """
        Give Gemini the real HK URLs + the pricing text and ask it to match them.
        URLs are pre-supplied so cannot be hallucinated.
        """
        url_list = "\n".join(f"  - {u}" for u in hk_urls)
        prompt   = _PRICE_MATCH_PROMPT.format(facts=facts, url_list=url_list)
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=self._extract_config,
        )
        data = _extract_json(_collect_text(response))
        return data.get("hk_retailers", [])

    # ── Step 3: URL validation ────────────────────────────────────────────────

    def _validate_retailers(
        self, raw_listings: list[dict]
    ) -> list[RetailerListing]:
        validated: list[RetailerListing] = []
        for item in raw_listings:
            url = item.get("url")
            if not url or url in ("null", "None", ""):
                log.info("  Skipping no-URL entry: %s", item.get("retailer_name"))
                continue
            log.info("  Checking URL: %s", url)
            if _url_is_reachable(url):
                try:
                    validated.append(RetailerListing(**item))
                    log.info(
                        "  ✔ %s  %s",
                        item.get("retailer_name"), item.get("price_hkd"),
                    )
                except Exception as exc:
                    log.warning("  Malformed entry: %s – %s", item, exc)
            else:
                log.info("  ✘ Dead URL: %s", url)
            if len(validated) == _MAX_RETAILERS:
                break
        return validated

    # ── Product info ──────────────────────────────────────────────────────────

    def _fetch_product_info(self, barcode: str) -> dict:
        log.info("Phase 1 – product details (barcode=%s) …", barcode)
        facts, _ = self._grounded_search(
            _PRODUCT_SEARCH_PROMPT.format(barcode=barcode)
        )
        return self._extract_product_json(facts)

    # ── HK retailer search ────────────────────────────────────────────────────

    def _fetch_hk_retailers(
        self, barcode: str, product_name: str, brand: str
    ) -> list[RetailerListing]:
        valid: list[RetailerListing] = []

        for attempt in range(1 + _EXTRA_SEARCH_ROUNDS):
            if attempt > 0:
                log.info(
                    "Retailer search round %d (valid so far: %d) …",
                    attempt + 1, len(valid),
                )
                time.sleep(1)

            log.info("Phase 2 – HK retailer search (attempt %d) …", attempt + 1)
            facts, all_urls = self._grounded_search(
                _RETAILER_SEARCH_PROMPT.format(
                    product_name=product_name,
                    brand=brand or "",
                    barcode=barcode,
                )
            )

            # ── Key fix: filter grounding URLs to HK retailers ────────────
            hk_urls = _filter_hk_retailers(all_urls)
            log.info(
                "Grounding URLs: %d total → %d matched HK retailers: %s",
                len(all_urls), len(hk_urls), hk_urls,
            )

            if hk_urls:
                # Use real grounding URLs + text for price matching
                raw_listings = self._match_prices_to_urls(facts, hk_urls)
            else:
                # No HK URLs in grounding — fall back to text-only extraction
                log.warning(
                    "No HK retailer URLs found in grounding metadata. "
                    "Falling back to text extraction (URLs may be less reliable)."
                )
                fallback_prompt = (
                    _PRICE_MATCH_PROMPT
                    .replace("Real product page URLs found by Google Search:\n{url_list}", "")
                    .replace("- Use the URLs EXACTLY as given — do not modify or construct new ones.\n- ", "")
                )
                # Simple text extraction without pre-supplied URLs
                extract_prompt = (
                    f"From these research notes about HK pet food retailers, "
                    f"extract retailer name, price in HKD, and product URL.\n\n"
                    f"Notes:\n{facts}\n\n"
                    f"Return JSON: {{\"hk_retailers\": [{{\"retailer_name\": \"...\", "
                    f"\"url\": \"...\", \"price_hkd\": \"...\", \"in_stock\": null, \"notes\": \"\"}}]}}"
                )
                response = self._client.models.generate_content(
                    model=self._model_name,
                    contents=extract_prompt,
                    config=self._extract_config,
                )
                data = _extract_json(_collect_text(response))
                raw_listings = data.get("hk_retailers", [])

            new_valid = self._validate_retailers(raw_listings)

            # Merge deduplicating by URL
            existing = {r.url for r in valid}
            for r in new_valid:
                if r.url not in existing:
                    valid.append(r)
                    existing.add(r.url)

            if len(valid) >= _MIN_RETAILERS:
                break

        if len(valid) < _MIN_RETAILERS:
            log.warning(
                "Only %d valid HK retailer(s) found after %d attempts.",
                len(valid), 1 + _EXTRA_SEARCH_ROUNDS,
            )

        return valid[:_MAX_RETAILERS]

    # ── Public API ────────────────────────────────────────────────────────────

    def search(self, barcode: str) -> ProductInfo:
        prod_data = self._fetch_product_info(barcode)

        product_name  = prod_data.get("product_name") or "Unknown Product"
        brand         = prod_data.get("brand") or ""
        target_animal = prod_data.get("target_animal") or ""
        mfr_url       = prod_data.get("manufacturer_url")
        image_url     = prod_data.get("image_url")

        nutri_raw = prod_data.get("nutritional_info") or {}
        other = {}
        if isinstance(nutri_raw, dict):
            other = nutri_raw.pop("other", {}) or {}
        try:
            nutritional = NutritionalInfo(**nutri_raw, other=other)
        except Exception:
            nutritional = NutritionalInfo()

        retailers = self._fetch_hk_retailers(barcode, product_name, brand)

        return ProductInfo(
            barcode=barcode,
            product_name=product_name,
            brand=brand or None,
            target_animal=target_animal or None,
            manufacturer_url=mfr_url,
            image_url=image_url,
            nutritional_info=nutritional,
            hk_retailers=retailers,
        )