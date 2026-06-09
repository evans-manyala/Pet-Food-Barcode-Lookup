"""
llm_searcher.py – Gemini via Vertex AI + Google Search Grounding.

Refined goal
------------
Avoid mixed barcode results by separating PRODUCT IDENTITY from RETAILER SEARCH.

The previous logic allowed Gemini to infer a product when exact barcode evidence
was weak. This version only returns/saves a product as verified when the search
notes explicitly connect the requested barcode (or its UPC/EAN variant) to the
product. Otherwise it returns "Unknown Product" and refuses to search retailers
or poison Redis/Pinecone with the wrong identity.

Pipeline
--------
  Step 1  Global exact barcode product identity search
          - Product name/brand/animal/size require barcode/SKU evidence
  Step 2  Global nutrition search
          - Manufacturer, brand, distributor, catalog, retailer, or label evidence
  Step 3  Global product image search
          - Any reliable source; rejects logos/placeholders/icons/tiny images
  Step 4  Hong Kong retailer search only
          - Direct buy URL + visible HKD price required
  Step 5  Save only verified product identity to Redis/Pinecone
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)

import re
import socket
import time
import html as html_lib
from io import BytesIO
from typing import Iterable
from urllib.parse import urlparse, quote, urljoin

import requests as http_requests
from PIL import Image
from google import genai
from google.genai import types

from .config import get_settings
from .models import ProductInfo, NutritionalInfo, RetailerListing

log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

# These domains are search hints only. They are not a hard allow-list.
# Final HK retailer acceptance is evidence-based: HK signal + product match + HKD price.

_URL_TIMEOUT         = 10
_MIN_RETAILERS       = 1       # do not force hallucination just to reach 2
_MAX_RETAILERS       = 5
_EXTRA_SEARCH_ROUNDS = 2

_HK_RETAILER_DOMAINS: list[str] = [
    "hktvmall.com",
    "hktv.com.hk",
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
    "petwifi.com.hk",
    "petwifi.pet",
    "homeypaw.com",
    "petpethome.com.hk",
    "ourpetshophk.com",
    "petpetgroup.com.hk",
    "foreverpets.hk",
    "petko.com.hk",
    "epet.hk",
    "mypetstore.com.hk",
    "q-pets.com",
    "catiscat.com.hk",
    "perfectcompanion.com",
]

_MANUFACTURER_OR_REFERENCE_DOMAINS: list[str] = [
    "perfectcompanion.com",
    "cindysrecipe.com",
    "smartheartpetfood.com",
    "petwifi.com.hk",
    "petwifi.pet",
    "homeypaw.com",
    "petpethome.com.hk",
    "ourpetshophk.com",
]


_BLOCKED_SOURCE_DOMAINS: list[str] = [
    "vertexaisearch.cloud.google",
    "aiplatform.googleapis.com",
    "googleapis.com",
    "google.com",
    "www.google.com",
    "webcache.googleusercontent.com",
]

_MAX_CONTEXT_URLS = 8


def _clean_public_source_url(url: str | None) -> str | None:
    """
    Gemini grounding sometimes exposes Google/Vertex redirect URLs from the
    search-entry UI. Those are not real product evidence and can be extremely
    long, which can make the follow-up JSON extraction truncate or break.
    Keep only normal public source URLs.
    """
    if not url or not isinstance(url, str):
        return None

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return None

    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().lstrip("www.")
        if not host:
            return None

        if any(host == d or host.endswith("." + d) for d in _BLOCKED_SOURCE_DOMAINS):
            return None

        # Very long URLs are usually tracking/redirect URLs rather than useful evidence.
        if len(url) > 900:
            return None

        # Remove fragments to improve de-duplication but keep query strings.
        cleaned = parsed._replace(fragment="").geturl()
        return cleaned
    except Exception:
        return None


def _clean_url_list(urls: Iterable[str], limit: int = _MAX_CONTEXT_URLS) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for url in urls or []:
        u = _clean_public_source_url(url)
        if u and u not in seen:
            seen.add(u)
            cleaned.append(u)
        if len(cleaned) >= limit:
            break
    return cleaned


# ─── Barcode helpers ──────────────────────────────────────────────────────────

def _barcode_variants(barcode: str) -> list[str]:
    """
    Return safe lookup variants.

    UPC-A is often stored as EAN-13 with a leading zero and sometimes displayed
    without its leading UPC number-system digit. Never strip arbitrary leading
    digits; only add/remove the known UPC/EAN leading-zero variants.
    """
    digits = re.sub(r"\D", "", barcode or "")
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


def _quoted_variants(barcode: str) -> str:
    return ", ".join(f'"{v}"' for v in _barcode_variants(barcode))


# ─── Robust JSON extraction ───────────────────────────────────────────────────

def _repair_and_parse(text: str) -> dict:
    """
    Try several repair strategies to turn LLM output into valid JSON.
    Handles:
      - Markdown fences
      - Prose before/after JSON
      - Trailing commas
      - Python literals
      - Simple single-quoted JSON-like strings
    """
    text = re.sub(r"```(?:json|JSON)?", "", text or "").strip().rstrip("`").strip()

    start = text.find("{")
    if start < 0:
        raise ValueError(f"No JSON object found:\n{text[:500]}")

    depth, in_str, esc, end = 0, False, False, -1
    for i, ch in enumerate(text[start:], start):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"' and not esc:
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    candidate = text[start:end] if end > 0 else text[start:]

    attempts = []
    attempts.append(candidate)
    attempts.append(re.sub(r",\s*([}\]])", r"\1", candidate))
    attempts.append(
        re.sub(r"\bNone\b", "null",
        re.sub(r"\bFalse\b", "false",
        re.sub(r"\bTrue\b", "true", attempts[-1])))
    )
    attempts.append(re.sub(r"(?<![\\])'", '"', attempts[-1]))

    for candidate_text in attempts:
        try:
            return json.loads(candidate_text)
        except json.JSONDecodeError:
            continue

    try:
        import json5  # type: ignore
        return json5.loads(candidate)
    except Exception:
        pass

    raise ValueError(
        "Could not parse JSON after all repair attempts. "
        f"Raw candidate (first 600 chars):\n{candidate[:600]}"
    )


def _extract_json(text: str) -> dict:
    return _repair_and_parse(text)


# ─── Response collectors ──────────────────────────────────────────────────────

def _collect_text(response) -> str:
    try:
        t = response.text
        if t:
            return t
    except Exception:
        pass

    chunks: list[str] = []
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
    Extract source URLs from Gemini grounding metadata.
    The exact object structure differs across SDK/model versions, so this tries
    known paths and de-duplicates.
    """
    seen: set[str] = set()
    urls: list[str] = []

    def _add(uri: str | None) -> None:
        cleaned = _clean_public_source_url(uri)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)

    for candidate in getattr(response, "candidates", []) or []:
        meta = getattr(candidate, "grounding_metadata", None)
        if meta is None:
            continue

        for chunk in getattr(meta, "grounding_chunks", []) or []:
            web = getattr(chunk, "web", None)
            _add(getattr(web, "uri", None) if web else None)

        for rm in getattr(meta, "retrieval_metadata", []) or []:
            source = getattr(rm, "source", None)
            _add(getattr(source, "uri", None) if source else None)

        sep = getattr(meta, "search_entry_point", None)
        if sep:
            rendered = getattr(sep, "rendered_content", "") or ""
            for href in re.findall(r'href=["\']([^"\']+)["\']', rendered):
                _add(href)

    return urls


def _grounding_search_queries(response) -> list[str]:
    queries: list[str] = []
    for candidate in getattr(response, "candidates", []) or []:
        meta = getattr(candidate, "grounding_metadata", None)
        if meta and getattr(meta, "web_search_queries", None):
            queries.extend(meta.web_search_queries)
    return queries


# ─── URL filtering / validation ───────────────────────────────────────────────

def _host_matches(url: str, domains: Iterable[str]) -> bool:
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        return any(host == d or host.endswith("." + d) for d in domains)
    except Exception:
        return False


def _filter_urls(urls: list[str], domains: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if _host_matches(url, domains) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _looks_like_product_url(url: str) -> bool:
    try:
        path = urlparse(url).path.strip("/").lower()
        if not path or path in {"en", "zh", "tc", "hk", "products", "product", "search"}:
            return False
        if "search" in path and "query" in urlparse(url).query.lower():
            return False
        return len(path) >= 5
    except Exception:
        return False


_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-HK;q=0.8,zh;q=0.7",
}

_NOT_FOUND_MARKERS = [
    "this product does not exist",
    "product does not exist",
    "this product is not available",
    "找不到網頁",
    "您欲連結的頁面已移除",
    "刪除或已不存在",
    "此商品不存在",
    "商品不存在",
    "不存在",
    "404 not found",
    "page not found",
]

_GENERIC_STOPWORDS = {
    "the", "and", "for", "with", "from", "food", "foods", "pet", "pets",
    "cat", "cats", "dog", "dogs", "adult", "kitten", "puppy", "original",
    "recipe", "formula", "can", "cans", "pack", "packs", "wet", "dry",
    "grain", "free", "fresh", "meal", "product", "products", "official",
}


def _normalise_match_text(value: str | None) -> str:
    value = (value or "").lower()
    value = re.sub(r"&amp;", "&", value)
    value = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _identity_tokens(product_name: str | None, brand: str | None) -> list[str]:
    """
    Build product-specific match tokens dynamically from the verified product
    identity. This replaces the previous barcode-specific hardcoded keyword list.
    """
    text = _normalise_match_text(f"{brand or ''} {product_name or ''}")
    tokens: list[str] = []

    # English / alphanumeric tokens
    for tok in re.findall(r"[a-z0-9]{3,}", text):
        if tok not in _GENERIC_STOPWORDS and tok not in tokens:
            tokens.append(tok)

    # Chinese chunks, useful for HK pages
    for tok in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if tok not in tokens:
            tokens.append(tok)

    return tokens[:18]


def _has_hk_signal(url: str, text: str) -> bool:
    """
    Evidence-based Hong Kong retailer detection.

    Do NOT rely on a hardcoded domain whitelist. A site is treated as an HK
    retail candidate only when the URL/page/snippet proves an HK signal:
      - .hk / .com.hk / .net.hk domain, OR
      - visible HKD/HK$/港幣/香港/Hong Kong/cart text in page/snippet.

    Known store names/domains may be used in prompts as search hints, but this
    validator is deliberately evidence-based so new HK stores can pass without
    code changes.
    """
    try:
        host = urlparse(url or "").netloc.lower().lstrip("www.")
    except Exception:
        host = ""

    plain = _strip_html(text or "").lower()

    if host.endswith((".hk", ".com.hk", ".net.hk", ".org.hk")):
        return True

    hk_markers = [
        "hk$", "hkd", "hong kong", "hongkong", "香港", "港幣",
        "加入購物車", "立即購買", "送貨", "本地配送", "滿$", "滿 hk",
        "hktvmall", "hong kong delivery",
    ]
    return any(marker in plain for marker in hk_markers)



# ─── SerpAPI budget + file cache ──────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    """Read an integer environment setting safely."""
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


class SerpApiBudget:
    """
    Per-barcode SerpAPI budget guard.

    This prevents one lookup from consuming many free-plan searches. Defaults are:
      - 2 Google AI Mode calls (identity rescue: global then HK)
      - 2 Google Search calls (barcode identity fallback)
      - 1 Google Images call
      - 3 Google Search calls for HK retailers
      - 7 total SerpAPI calls

    Override in .env if needed:
      SERPAPI_AI_MODE_MAX_CALLS=2
      SERPAPI_IDENTITY_SEARCH_MAX_CALLS=2
      SERPAPI_IMAGE_MAX_CALLS=1
      SERPAPI_HK_MAX_CALLS=3
      SERPAPI_TOTAL_MAX_CALLS=7
    """
    def __init__(self) -> None:
        self.total_calls = 0
        self.ai_mode_calls = 0
        self.identity_search_calls = 0
        self.image_calls = 0
        self.hk_calls = 0
        self.max_total = _env_int("SERPAPI_TOTAL_MAX_CALLS", 7)
        self.max_ai_mode = _env_int("SERPAPI_AI_MODE_MAX_CALLS", 2)
        self.max_identity_search = _env_int("SERPAPI_IDENTITY_SEARCH_MAX_CALLS", 2)
        self.max_image = _env_int("SERPAPI_IMAGE_MAX_CALLS", 1)
        self.max_hk = _env_int("SERPAPI_HK_MAX_CALLS", 3)

    def can_call_ai_mode(self) -> bool:
        return self.total_calls < self.max_total and self.ai_mode_calls < self.max_ai_mode

    def can_call_identity_search(self) -> bool:
        return (
            self.total_calls < self.max_total
            and self.identity_search_calls < self.max_identity_search
        )

    def can_call_image(self) -> bool:
        return self.total_calls < self.max_total and self.image_calls < self.max_image

    def can_call_hk(self) -> bool:
        return self.total_calls < self.max_total and self.hk_calls < self.max_hk

    def _log_budget(self) -> None:
        log.info(
            "SerpAPI budget used: ai_mode=%d/%d identity_search=%d/%d "
            "image=%d/%d hk=%d/%d total=%d/%d",
            self.ai_mode_calls, self.max_ai_mode,
            self.identity_search_calls, self.max_identity_search,
            self.image_calls, self.max_image,
            self.hk_calls, self.max_hk,
            self.total_calls, self.max_total,
        )

    def record_ai_mode(self) -> None:
        self.total_calls += 1
        self.ai_mode_calls += 1
        self._log_budget()

    def record_identity_search(self) -> None:
        self.total_calls += 1
        self.identity_search_calls += 1
        self._log_budget()

    def record_image(self) -> None:
        self.total_calls += 1
        self.image_calls += 1
        self._log_budget()

    def record_hk(self) -> None:
        self.total_calls += 1
        self.hk_calls += 1
        self._log_budget()


def _serpapi_cache_enabled() -> bool:
    return os.getenv("SERPAPI_CACHE_ENABLED", "1").strip().lower() not in {"0", "false", "no"}


def _serpapi_cache_dir() -> Path:
    d = PROJECT_ROOT / ".cache" / "serpapi"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _serpapi_cache_key(kind: str, barcode: str, product_name: str) -> Path:
    digits = re.sub(r"\D", "", barcode or "") or "unknown"
    safe_name = re.sub(r"[^a-z0-9]+", "-", _normalise_match_text(product_name or ""))[:90].strip("-")
    if not safe_name:
        safe_name = "product"
    return _serpapi_cache_dir() / f"{kind}_{digits}_{safe_name}.json"


def _read_serpapi_cache(kind: str, barcode: str, product_name: str, ttl_seconds: int):
    if not _serpapi_cache_enabled():
        return None
    path = _serpapi_cache_key(kind, barcode, product_name)
    try:
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > ttl_seconds:
            return None
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        log.info("SerpAPI %s cache HIT: %s", kind, path.name)
        return data
    except Exception as exc:
        log.debug("SerpAPI cache read failed for %s: %s", path, exc)
        return None


def _write_serpapi_cache(kind: str, barcode: str, product_name: str, data) -> None:
    if not _serpapi_cache_enabled():
        return
    path = _serpapi_cache_key(kind, barcode, product_name)
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log.info("SerpAPI %s cache SET: %s", kind, path.name)
    except Exception as exc:
        log.debug("SerpAPI cache write failed for %s: %s", path, exc)


def _build_image_queries(product_name: str, brand: str, barcode: str) -> list[str]:
    """Staged image queries. Usually only the first query runs because of budget."""
    clean_product = re.sub(r"\s+", " ", product_name or "").strip()
    clean_brand = re.sub(r"\s+", " ", brand or "").strip()
    clean_barcode = re.sub(r"\D", "", barcode or "")

    queries = []
    if clean_product and clean_brand:
        queries.append(f'"{clean_product}" "{clean_brand}" product image')
    elif clean_product:
        queries.append(f'"{clean_product}" product image')

    # Fallback only if budget allows more than one image query.
    if clean_barcode and clean_product:
        queries.append(f'"{clean_barcode}" "{clean_product}" image')

    return list(dict.fromkeys(q for q in queries if q.strip()))


def _build_hk_search_queries(product_name: str, brand: str, barcode: str) -> list[str]:
    """Staged HK retailer queries based on verified product identity, not hardcoded terms."""
    clean_product = re.sub(r"\s+", " ", product_name or "").strip()
    clean_brand = re.sub(r"\s+", " ", brand or "").strip()

    noise_filter = (
        "-finance -stock -directory -membership -broker -insurance -hkex "
        "-hktdc -kompass -yahoo -chamber -company -profile -linkedin"
    )

    tokens = _identity_tokens(clean_product, clean_brand)
    generic_query_stop = _GENERIC_STOPWORDS | {"brand"}
    distinctive = [t for t in tokens if t not in generic_query_stop][:6]
    distinctive_query = " ".join(f'"{t}"' for t in distinctive)

    queries: list[str] = []

    # Product-name queries first: these are less noisy than barcode-only queries.
    if clean_product:
        queries.extend([
            f'"{clean_product}" "HK$" pet food {noise_filter}',
            f'"{clean_product}" "香港" pet food {noise_filter}',
        ])

    if clean_brand and distinctive_query:
        queries.append(f'"{clean_brand}" {distinctive_query} "HK$" "Hong Kong" pet food {noise_filter}')

    # Exact barcode fallback. Useful when HK retailers show SKU/barcode, but kept after product queries.
    for v in _barcode_variants(barcode):
        queries.extend([
            f'"{v}" "HK$" pet food {noise_filter}',
            f'"{v}" "香港" pet food {noise_filter}',
        ])

    # A broad HK pet shop fallback, only used if budget allows.
    if clean_brand and distinctive_query:
        queries.append(f'"{clean_brand}" {distinctive_query} "Hong Kong pet shop" {noise_filter}')

    return list(dict.fromkeys(q for q in queries if q.strip()))
def _serpapi_google_image_candidates(
    product_name: str,
    brand: str,
    barcode: str,
    max_results: int | None = None,
    budget: SerpApiBudget | None = None,
    use_cache: bool = True,
) -> list[dict]:
    """
    Low-cost SerpAPI Google Images discovery layer.

    Cost controls:
    - Uses a short-lived file cache so repeated local tests do not burn calls.
    - Runs only SERPAPI_IMAGE_MAX_CALLS queries, default 1.
    - One Google Images call usually returns enough candidates.
    """
    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        log.warning("SERPAPI_API_KEY is missing. SerpAPI image search skipped.")
        return []

    max_results = max_results or _env_int("SERPAPI_IMAGE_MAX_CANDIDATES", 20)
    cache_ttl = _env_int("SERPAPI_IMAGE_CACHE_TTL", 7 * 86400)

    if use_cache:
        cached = _read_serpapi_cache("image", barcode, product_name, cache_ttl)
        if isinstance(cached, list):
            return cached[:max_results]

    queries = _build_image_queries(product_name, brand, barcode)
    if not queries:
        return []

    max_calls = _env_int("SERPAPI_IMAGE_MAX_CALLS", 1)
    candidates: list[dict] = []
    seen: set[str] = set()
    calls_made = 0

    for query in queries:
        if calls_made >= max_calls:
            break
        if budget and not budget.can_call_image():
            log.info("SerpAPI image budget exhausted; skipping remaining image queries.")
            break

        try:
            log.info("SerpAPI image search query: %s", query)
            if budget:
                budget.record_image()
            calls_made += 1

            resp = http_requests.get(
                "https://serpapi.com/search",
                params={
                    "engine": "google_images",
                    "q": query,
                    "api_key": api_key,
                    "hl": "en",
                    "gl": "us",
                    "ijn": 0,
                    "safe": "active",
                },
                timeout=30,
            )
            log.info("SerpAPI image HTTP %s", resp.status_code)
            if resp.status_code >= 400:
                log.warning("SerpAPI image error body: %s", resp.text[:500])
                continue

            data = resp.json()
            if data.get("error"):
                log.warning("SerpAPI image API error: %s", data.get("error"))
                continue

            results = data.get("images_results") or []
            log.info("SerpAPI image results count: %d", len(results))

            for pos, item in enumerate(results, start=1):
                image_url = item.get("original") or item.get("thumbnail")
                source_url = item.get("link") or item.get("source_page") or item.get("original")
                if not image_url or image_url in seen:
                    continue
                seen.add(image_url)
                candidates.append({
                    "position": pos,
                    "image_url": image_url,
                    "source_url": source_url,
                    "title": item.get("title") or "",
                    "source": item.get("source") or "",
                    "query": query,
                    "provider": "serpapi_google_images",
                })
                if len(candidates) >= max_results:
                    break

            # Early stop: if one query gave candidates, let Python/Gemini validation decide.
            # This avoids running 3–5 extra image searches for every barcode.
            if candidates:
                break

        except Exception as exc:
            log.warning("SerpAPI image search failed for query=%s: %s", query, exc)

    candidates = candidates[:max_results]
    if candidates:
        _write_serpapi_cache("image", barcode, product_name, candidates)
    log.info("SerpAPI image candidates collected: %d", len(candidates))
    return candidates


def _domain_resolves(url: str) -> bool:
    """Return True only when the candidate host resolves in DNS."""
    try:
        host = urlparse(url or "").netloc.lower().split(":")[0]
        if not host:
            return False
        # Try both exact and without www. to handle common aliases.
        hosts = [host]
        if host.startswith("www."):
            hosts.append(host[4:])
        else:
            hosts.append("www." + host)
        for h in hosts:
            try:
                socket.getaddrinfo(h, None)
                return True
            except socket.gaierror:
                continue
        return False
    except Exception:
        return False


_NON_PRODUCT_IMAGE_MARKERS = [
    "logo", "icon", "favicon", "seal", "seals", "certified", "certificate",
    "certification", "trust", "secure", "security", "payment", "visa",
    "mastercard", "paypal", "badge", "banner", "spinner", "loading",
    "placeholder", "noimage", "no-image", "blank", "pixel", "sprite",
    "avatar", "social", "facebook", "instagram", "whatsapp", "legitscript",
    "trustpilot", "verified", "footer", "header", "widget", "app-store",
    "google-play", "qrcode", "qr-code", "gift-card", "coupon", "promo",
]

_NON_PRODUCT_IMAGE_HOST_MARKERS = [
    "legitscript", "trustpilot", "paypal", "visa", "mastercard",
    "facebook", "instagram", "google", "gstatic", "doubleclick",
]


def _image_negative_asset_penalty(image_url: str, title: str = "", source_url: str = "") -> int:
    """Generic non-product asset penalty; not a brand/domain whitelist."""
    text = _normalise_match_text(f"{image_url} {title} {source_url}")
    host = urlparse(image_url or "").netloc.lower()
    penalty = 0
    if any(marker in text for marker in _NON_PRODUCT_IMAGE_MARKERS):
        penalty += 100
    if any(marker in host for marker in _NON_PRODUCT_IMAGE_HOST_MARKERS):
        penalty += 100
    path = urlparse(image_url or "").path.lower()
    filename = path.rstrip("/").split("/")[-1]
    if filename in {"1.png", "0.png", "blank.png", "transparent.png", "loading.gif", "loader.gif", "spacer.gif"}:
        penalty += 100
    return penalty


def _image_positive_product_score(
    image_url: str,
    title: str,
    source_url: str,
    product_name: str,
    brand: str,
    barcode: str,
) -> int:
    """Score positive evidence that an image is the actual product package."""
    text = _normalise_match_text(f"{image_url} {title} {source_url}")
    tokens = _identity_tokens(product_name, brand)
    score = 0

    brand_norm = _normalise_match_text(brand)
    if brand_norm and brand_norm in text:
        score += 25

    token_hits = sum(1 for token in tokens if token in text)
    score += token_hits * 8

    digits = re.sub(r"\D", "", barcode or "")
    if digits and digits in re.sub(r"\D", "", text):
        score += 30

    path = urlparse(image_url or "").path.lower()
    if re.search(r"\.(jpg|jpeg|png|webp)(?:$|\?)", path):
        score += 10

    if any(x in text for x in ["product", "pack", "bag", "can", "pouch", "dry", "wet", "food", "cat", "dog"]):
        score += 10

    return score


def _download_image_for_validation(image_url: str) -> tuple[bytes | None, str | None, str | None, tuple[int, int] | None]:
    """Download image, verify content type and dimensions."""
    try:
        resp = http_requests.get(
            image_url,
            headers=_HTTP_HEADERS,
            timeout=20,
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            return None, None, None, None
        content_type = resp.headers.get("content-type", "").split(";")[0].lower()
        if "image/" not in content_type:
            return None, None, None, None
        data = resp.content or b""
        if len(data) < 8_000:
            return None, None, None, None
        try:
            img = Image.open(BytesIO(data))
            width, height = img.size
            img.verify()
        except Exception:
            return None, None, None, None
        if width < 180 or height < 180 or (width * height) < 40_000:
            return None, None, None, None
        return data, resp.url, content_type or "image/jpeg", (width, height)
    except Exception:
        return None, None, None, None


def _is_candidate_image_valid(
    candidate: dict,
    product_name: str,
    brand: str,
    barcode: str,
) -> tuple[bool, str | None, bytes | None, str | None, int]:
    """
    Validate image URL with dimensions + product-evidence scoring.

    Scoring is generic and scalable:
    - positive evidence comes from verified product tokens, barcode, source title,
      source page, SerpAPI rank, and official/brand-looking hosts;
    - negative evidence rejects non-product assets such as seals, logos,
      payment badges, placeholders, icons, and social widgets.
    """
    image_url = str(candidate.get("image_url") or "").strip()
    if not image_url:
        return False, None, None, None, -999

    title = str(candidate.get("title") or candidate.get("reason") or "")
    source_url = str(candidate.get("source_url") or "")

    if _image_negative_asset_penalty(image_url, title, source_url) >= 100:
        return False, None, None, None, -999

    image_bytes, final_url, mime_type, dims = _download_image_for_validation(image_url)
    if not image_bytes or not final_url or not mime_type or not dims:
        return False, None, None, None, -999

    positive = _image_positive_product_score(final_url, title, source_url, product_name, brand, barcode)
    penalty = _image_negative_asset_penalty(final_url, title, source_url)

    # Prefer higher-ranked SerpAPI results. This makes the Google Images order
    # useful instead of choosing a random CDN image with slightly more tokens.
    try:
        position = int(candidate.get("position") or 0)
    except Exception:
        position = 0
    if position == 1:
        positive += 45
    elif 2 <= position <= 3:
        positive += 30
    elif 4 <= position <= 5:
        positive += 15

    # Prefer official/brand domains generically. This is not a hardcoded brand
    # whitelist; it derives tokens from the verified brand name.
    brand_tokens = [
        t for t in re.findall(r"[a-z0-9]{4,}", _normalise_match_text(brand))
        if t not in _GENERIC_STOPWORDS
    ]
    source_host = urlparse(source_url or "").netloc.lower().lstrip("www.")
    image_host = urlparse(final_url or "").netloc.lower().lstrip("www.")

    if any(t in source_host for t in brand_tokens):
        positive += 50
    if any(t in image_host for t in brand_tokens):
        positive += 35

    # Manufacturer/catalog/product media paths are useful positive signals.
    final_url_low = final_url.lower()
    if any(x in final_url_low for x in ["/products/", "/product/", "/media/", "/catalog/", "/shop/"]):
        positive += 10

    width, height = dims
    if width >= 400 and height >= 400:
        positive += 10

    score = positive - penalty
    if score < 25:
        return False, None, None, None, score

    return True, final_url, image_bytes, mime_type, score


def _official_brand_tokens(brand: str | None) -> list[str]:
    """
    Dynamic brand tokens used to prefer official/manufacturer image sources.

    This is generic. It does not hardcode Purina, Royal Canin, Hill's, Cindy's,
    or any other brand. If the verified brand is "Purina Pro Plan", tokens like
    "purina" and "plan" are derived automatically, then source/image hosts such
    as purina.com are preferred over retailer/CDN hosts.
    """
    tokens = [
        t for t in re.findall(r"[a-z0-9]{4,}", _normalise_match_text(brand or ""))
        if t not in _GENERIC_STOPWORDS
    ]
    return list(dict.fromkeys(tokens))[:6]


def _is_official_brand_image_candidate(
    candidate: dict,
    final_url: str,
    product_name: str,
    brand: str,
) -> bool:
    """
    Prefer official/brand/manufacturer-looking image candidates.

    The rule is evidence-based and scalable:
      - derive brand tokens from the verified brand name;
      - check if those tokens appear in the source-page host or image host;
      - only use this as a preference after the image has already passed product
        image validation.
    """
    brand_tokens = _official_brand_tokens(brand)
    if not brand_tokens:
        return False

    source_url = str(candidate.get("source_url") or "")
    source_host = urlparse(source_url).netloc.lower().lstrip("www.")
    image_host = urlparse(final_url or "").netloc.lower().lstrip("www.")

    if any(t in source_host for t in brand_tokens):
        return True
    if any(t in image_host for t in brand_tokens):
        return True

    return False


def _candidate_brand_source_bonus(candidate: dict, final_url: str, brand: str) -> int:
    """Small generic score bonus for official-looking image candidates."""
    brand_tokens = _official_brand_tokens(brand)
    if not brand_tokens:
        return 0

    source_url = str(candidate.get("source_url") or "")
    source_host = urlparse(source_url).netloc.lower().lstrip("www.")
    image_host = urlparse(final_url or "").netloc.lower().lstrip("www.")

    bonus = 0
    if any(t in source_host for t in brand_tokens):
        bonus += 500
    if any(t in image_host for t in brand_tokens):
        bonus += 300
    return bonus


def _serpapi_hk_product_page_candidates(
    product_name: str,
    brand: str,
    barcode: str,
    max_results: int | None = None,
    budget: SerpApiBudget | None = None,
    use_cache: bool = True,
) -> tuple[list[dict], str]:
    """
    Low-cost SerpAPI Google Search discovery for HK buy pages.

    This is candidate discovery only. Final acceptance still requires:
      direct product page + live page opens + product match + HK signal + HKD price.

    Cost controls:
    - Uses file cache with TTL.
    - Runs only SERPAPI_HK_MAX_CALLS queries, default 3.
    - Stops early once enough plausible candidates are found.
    """
    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        log.warning("SERPAPI_API_KEY is missing. SerpAPI HK URL search skipped.")
        return [], ""

    max_results = max_results or _env_int("SERPAPI_HK_MAX_CANDIDATES", 15)
    cache_ttl = _env_int("SERPAPI_HK_CACHE_TTL", 24 * 3600)

    if use_cache:
        cached = _read_serpapi_cache("hk", barcode, product_name, cache_ttl)
        if isinstance(cached, dict):
            listings = cached.get("listings") or []
            notes = cached.get("notes") or ""
            return listings[:max_results], notes

    clean_product = re.sub(r"\s+", " ", product_name or "").strip()
    clean_brand = re.sub(r"\s+", " ", brand or "").strip()
    product_tokens = _identity_tokens(clean_product, clean_brand)
    barcode_variants = [re.sub(r"\D", "", v) for v in _barcode_variants(barcode)]

    queries = _build_hk_search_queries(clean_product, clean_brand, barcode)
    max_calls = _env_int("SERPAPI_HK_MAX_CALLS", 3)

    blocked_hosts = [
        "finance.yahoo.com", "hkex.com.hk", "hktdc.com", "kompass.com",
        "chamber.org.hk", "irasia.com", "aeodirectory.com",
        "hongkonginsurancebrokers.com", "dutchchamber.hk", "webbsite",
        "linkedin.com", "facebook.com", "instagram.com",
    ]

    listings: list[dict] = []
    seen: set[str] = set()
    notes: list[str] = []
    calls_made = 0

    def add_candidate(url: str | None, title: str, snippet: str, rich=None, query: str = "") -> None:
        if not url or url in seen:
            return
        host = urlparse(url).netloc.lower().lstrip("www.")
        if any(b in host for b in blocked_hosts):
            return
        if not _looks_like_product_url(url):
            return

        context = f"{title} {snippet} {json.dumps(rich or {}, ensure_ascii=False)} {url}"
        context_norm = _normalise_match_text(context)
        digits_context = re.sub(r"\D", "", context_norm)

        barcode_hit = any(v and v in digits_context for v in barcode_variants)
        token_hits = sum(1 for t in product_tokens if t in context_norm)
        required_hits = 2 if len(product_tokens) <= 5 else 3

        commerce_context = any(x in context_norm for x in [
            "cat", "dog", "pet", "feline", "canine", "food", "treat",
            "formula", "diet", "nutrition", "shop", "store", "cart", "price",
            "buy", "hkd", "hk", "hong kong", "香港", "港幣", "加入購物車", "立即購買",
            "hktvmall", "vetopia", "epet",
        ])

        if not _has_hk_signal(url, context):
            return
        if not commerce_context:
            return
        if not barcode_hit and token_hits < required_hits:
            return

        price = _extract_hkd_price(context)
        seen.add(url)
        listings.append({
            "retailer_name": host,
            "url": url,
            "price_hkd": price,
            "in_stock": None,
            "notes": f"Discovered by SerpAPI query: {query}. Snippet: {snippet[:240]}",
        })
        notes.append(f"{title}\n{url}\n{snippet}\n")

    for query in queries:
        if calls_made >= max_calls:
            break
        if budget and not budget.can_call_hk():
            log.info("SerpAPI HK budget exhausted; skipping remaining HK queries.")
            break

        try:
            log.info("SerpAPI HK search query: %s", query)
            if budget:
                budget.record_hk()
            calls_made += 1

            resp = http_requests.get(
                "https://serpapi.com/search",
                params={
                    "engine": "google",
                    "q": query,
                    "api_key": api_key,
                    "hl": "en",
                    "gl": "hk",
                    "google_domain": "google.com.hk",
                    "num": 10,
                },
                timeout=30,
            )
            log.info("SerpAPI HK HTTP %s", resp.status_code)
            if resp.status_code >= 400:
                log.warning("SerpAPI HK error body: %s", resp.text[:500])
                continue

            data = resp.json()
            if data.get("error"):
                log.warning("SerpAPI HK API error: %s", data.get("error"))
                continue

            for item in data.get("organic_results", []) or []:
                add_candidate(
                    url=item.get("link"),
                    title=item.get("title") or "",
                    snippet=item.get("snippet") or "",
                    rich=item.get("rich_snippet") or {},
                    query=query,
                )
                if len(listings) >= max_results:
                    break

            # Same SerpAPI call may include shopping/product blocks. Parse them too at no extra cost.
            for block_name in ["shopping_results", "inline_shopping_results", "product_results"]:
                block = data.get(block_name) or []
                if isinstance(block, dict):
                    block = [block]
                for item in block:
                    url = item.get("link") or item.get("product_link") or item.get("serpapi_product_api")
                    title = item.get("title") or item.get("name") or ""
                    snippet = " ".join(str(item.get(k) or "") for k in ["snippet", "description", "price", "extracted_price", "source"])
                    add_candidate(url=url, title=title, snippet=snippet, rich=item, query=query)
                    if len(listings) >= max_results:
                        break
                if len(listings) >= max_results:
                    break

            if len(listings) >= max_results:
                break

            # Early stop if we already have enough candidates to validate.
            if len(listings) >= _env_int("SERPAPI_HK_EARLY_STOP_CANDIDATES", 6):
                break

        except Exception as exc:
            log.warning("SerpAPI HK URL search failed for query=%s: %s", query, exc)

    result_notes = "\n".join(notes)
    payload = {"listings": listings[:max_results], "notes": result_notes}
    if listings:
        _write_serpapi_cache("hk", barcode, product_name, payload)
    return listings[:max_results], result_notes


_AI_MODE_EMPTY_PHRASES = (
    "no response available",
    "try asking something else",
    "couldn't generate",
    "something went wrong",
    "can't help with that",
)


def _is_usable_ai_mode_facts(facts: str, urls: list[str]) -> bool:
    """Reject SerpAPI AI Mode placeholder/error text that contains no product evidence."""
    norm = (facts or "").strip().lower()
    if not norm and not urls:
        return False
    if any(phrase in norm for phrase in _AI_MODE_EMPTY_PHRASES):
        return False
    if len(norm) < 80 and not urls:
        return False
    return True


def _build_ai_mode_simple_query(barcode: str) -> str:
    """Short query that matches manual browser AI Mode searches."""
    return f"find the product with barcode {barcode} and its guaranteed analysis"


def _format_serpapi_organic_results_as_facts(data: dict, query: str) -> tuple[str, list[str]]:
    """Turn regular Google Search JSON into research text for Gemini extraction."""
    parts = [f"SerpAPI Google Search query: {query}"]
    urls: list[str] = []

    for item in data.get("organic_results") or []:
        title = (item.get("title") or "").strip()
        link = (item.get("link") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        if link:
            urls.append(link)
        if title or snippet or link:
            parts.append(f"{title}\n{snippet}\n{link}".strip())

    for block_name in ["shopping_results", "inline_shopping_results", "product_results"]:
        block = data.get(block_name) or []
        if isinstance(block, dict):
            block = [block]
        for item in block:
            title = (item.get("title") or item.get("name") or "").strip()
            link = (
                item.get("link")
                or item.get("product_link")
                or item.get("serpapi_product_api")
                or ""
            ).strip()
            snippet = " ".join(
                str(item.get(k) or "")
                for k in ["snippet", "description", "price", "extracted_price", "source"]
            ).strip()
            if link:
                urls.append(link)
            if title or snippet or link:
                parts.append(f"{title}\n{snippet}\n{link}".strip())

    kg = data.get("knowledge_graph") or {}
    if isinstance(kg, dict) and kg:
        kg_title = (kg.get("title") or "").strip()
        kg_desc = (kg.get("description") or "").strip()
        kg_link = ""
        for key in ("website", "source", "link"):
            val = kg.get(key)
            if isinstance(val, dict):
                kg_link = (val.get("link") or val.get("url") or "").strip()
            elif isinstance(val, str):
                kg_link = val.strip()
            if kg_link:
                break
        if kg_link:
            urls.append(kg_link)
        if kg_title or kg_desc:
            parts.append(f"Knowledge graph: {kg_title}\n{kg_desc}\n{kg_link}".strip())

    facts = "\n\n".join(p for p in parts if p)
    return facts, list(dict.fromkeys(urls))


def _parse_serpapi_ai_mode_response(data: dict) -> tuple[str, list[str], str]:
    """Extract research text, source URLs, and SerpAPI search id from AI Mode JSON."""
    parts: list[str] = []

    markdown = (data.get("reconstructed_markdown") or "").strip()
    if markdown:
        parts.append(markdown)

    for block in data.get("text_blocks") or []:
        snippet = (block.get("snippet") or "").strip()
        if snippet:
            parts.append(snippet)
        for item in block.get("list") or []:
            item_snippet = (item.get("snippet") or "").strip()
            if item_snippet:
                parts.append(item_snippet)
        for row in block.get("table") or []:
            if isinstance(row, list):
                parts.append(" | ".join(str(cell) for cell in row if cell))

    for qr in data.get("quick_results") or []:
        title = (qr.get("title") or "").strip()
        snippet = (qr.get("snippet") or "").strip()
        link = (qr.get("link") or "").strip()
        if title or snippet:
            parts.append(f"{title}: {snippet}".strip(": "))
        if link:
            parts.append(link)

    facts = "\n\n".join(p for p in parts if p)

    urls: list[str] = []
    for ref in data.get("references") or []:
        link = (ref.get("link") or "").strip()
        if link:
            urls.append(link)
        title = (ref.get("title") or "").strip()
        snippet = (ref.get("snippet") or "").strip()
        if title or snippet:
            facts += f"\n\nSource: {title}\n{snippet}\n{link}"

    search_id = str((data.get("search_metadata") or {}).get("id") or "")
    return facts, list(dict.fromkeys(urls)), search_id


def _serpapi_google_ai_mode_identity(
    barcode: str,
    budget: SerpApiBudget | None = None,
    use_cache: bool = True,
) -> tuple[str, list[str], str]:
    """
    SerpAPI Google AI Mode — identity rescue when Vertex AI grounding finds nothing.
    Tries a short global query first, then Hong Kong. Returns (facts_text, source_urls, search_id).
    """
    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        log.warning("SERPAPI_API_KEY is missing. SerpAPI Google AI Mode skipped.")
        return "", [], ""

    if budget and not budget.can_call_ai_mode():
        log.info("SerpAPI AI Mode budget exhausted; skipping identity rescue.")
        return "", [], ""

    cache_ttl = _env_int("SERPAPI_AI_MODE_CACHE_TTL", 24 * 3600)
    cache_label = f"identity_{barcode}"

    if use_cache:
        cached = _read_serpapi_cache("ai_mode", barcode, cache_label, cache_ttl)
        if isinstance(cached, dict):
            facts = cached.get("facts") or ""
            urls = cached.get("urls") or []
            if _is_usable_ai_mode_facts(facts, urls):
                return facts, urls, cached.get("search_id") or ""
            log.info("SerpAPI AI Mode cache hit rejected (empty/error response); retrying live.")

    query = _build_ai_mode_simple_query(barcode)
    hk_location = os.getenv("SERPAPI_AI_MODE_LOCATION", "Hong Kong")
    attempts = [
        ("global", "us", "google.com", "United States"),
        ("hk", "hk", "google.com.hk", hk_location),
    ]

    for label, gl, google_domain, location in attempts:
        if budget and not budget.can_call_ai_mode():
            log.info("SerpAPI AI Mode budget exhausted after %s attempt.", label)
            break

        try:
            log.info(
                "SerpAPI Google AI Mode identity query for barcode=%s (%s, %s)",
                barcode, label, google_domain,
            )
            if budget:
                budget.record_ai_mode()

            resp = http_requests.get(
                "https://serpapi.com/search",
                params={
                    "engine": "google_ai_mode",
                    "q": query,
                    "api_key": api_key,
                    "hl": "en",
                    "gl": gl,
                    "google_domain": google_domain,
                    "location": location,
                    "device": "desktop",
                },
                timeout=90,
            )
            log.info("SerpAPI Google AI Mode HTTP %s (%s)", resp.status_code, label)
            if resp.status_code >= 400:
                log.warning("SerpAPI AI Mode error body (%s): %s", label, resp.text[:500])
                continue

            data = resp.json()
            if data.get("error"):
                log.warning("SerpAPI AI Mode API error (%s): %s", label, data.get("error"))
                continue

            facts, urls, search_id = _parse_serpapi_ai_mode_response(data)
            log.info(
                "SerpAPI Google AI Mode (%s): %d chars | %d URLs | search_id=%s",
                label, len(facts), len(urls), search_id or "—",
            )

            if not _is_usable_ai_mode_facts(facts, urls):
                log.warning(
                    "SerpAPI AI Mode (%s) returned no usable product evidence for barcode=%s",
                    label, barcode,
                )
                continue

            _write_serpapi_cache("ai_mode", barcode, cache_label, {
                "facts": facts,
                "urls": urls,
                "search_id": search_id,
            })
            return facts, urls, search_id

        except Exception as exc:
            log.warning("SerpAPI Google AI Mode identity search failed (%s): %s", label, exc)

    return "", [], ""


def _serpapi_google_barcode_search_identity(
    barcode: str,
    budget: SerpApiBudget | None = None,
    use_cache: bool = True,
) -> tuple[str, list[str], str]:
    """
    Regular SerpAPI Google Search fallback when AI Mode returns nothing.
    Returns (facts_text, source_urls, comma-separated search_ids).
    """
    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        log.warning("SERPAPI_API_KEY is missing. SerpAPI barcode search skipped.")
        return "", [], ""

    if budget and not budget.can_call_identity_search():
        log.info("SerpAPI identity-search budget exhausted; skipping barcode fallback.")
        return "", [], ""

    cache_ttl = _env_int("SERPAPI_IDENTITY_SEARCH_CACHE_TTL", 24 * 3600)
    cache_label = f"search_{barcode}"

    if use_cache:
        cached = _read_serpapi_cache("identity", barcode, cache_label, cache_ttl)
        if isinstance(cached, dict):
            facts = cached.get("facts") or ""
            urls = cached.get("urls") or []
            if facts or urls:
                return facts, urls, cached.get("search_id") or ""

    queries = [f'"{barcode}"', f'"{barcode}" pet food']
    attempts = [
        ("global", "us", "google.com", "United States"),
        ("hk", "hk", "google.com.hk", os.getenv("SERPAPI_AI_MODE_LOCATION", "Hong Kong")),
    ]

    merged_facts: list[str] = []
    merged_urls: list[str] = []
    search_ids: list[str] = []

    for label, gl, google_domain, location in attempts:
        for query in queries:
            if budget and not budget.can_call_identity_search():
                log.info("SerpAPI identity-search budget exhausted.")
                break

            try:
                log.info(
                    "SerpAPI barcode search for barcode=%s (%s): %s",
                    barcode, label, query,
                )
                if budget:
                    budget.record_identity_search()

                resp = http_requests.get(
                    "https://serpapi.com/search",
                    params={
                        "engine": "google",
                        "q": query,
                        "api_key": api_key,
                        "hl": "en",
                        "gl": gl,
                        "google_domain": google_domain,
                        "location": location,
                        "num": 10,
                    },
                    timeout=45,
                )
                log.info("SerpAPI barcode search HTTP %s (%s)", resp.status_code, label)
                if resp.status_code >= 400:
                    log.warning("SerpAPI barcode search error body (%s): %s", label, resp.text[:500])
                    continue

                data = resp.json()
                if data.get("error"):
                    log.warning("SerpAPI barcode search API error (%s): %s", label, data.get("error"))
                    continue

                facts, urls = _format_serpapi_organic_results_as_facts(data, query)
                search_id = str((data.get("search_metadata") or {}).get("id") or "")
                if search_id:
                    search_ids.append(search_id)
                if facts:
                    merged_facts.append(facts)
                merged_urls.extend(urls)

                if merged_urls:
                    break
            except Exception as exc:
                log.warning("SerpAPI barcode search failed (%s, %s): %s", label, query, exc)

        if merged_urls:
            break

    facts_text = "\n\n---\n\n".join(merged_facts)
    unique_urls = list(dict.fromkeys(merged_urls))
    search_id_text = ",".join(search_ids)

    if facts_text or unique_urls:
        _write_serpapi_cache("identity", barcode, cache_label, {
            "facts": facts_text,
            "urls": unique_urls,
            "search_id": search_id_text,
        })

    log.info(
        "SerpAPI barcode search: %d chars | %d URLs | search_ids=%s",
        len(facts_text), len(unique_urls), search_id_text or "—",
    )
    return facts_text, unique_urls, search_id_text


def _gemini_vision_verify_product_image(
    self,
    image_bytes: bytes,
    product_name: str,
    brand: str,
    barcode: str,
) -> bool:
    """
    Ask Gemini Vision whether the candidate image appears to be the correct product.
    This does NOT discover images. It only verifies a candidate image.
    """
    try:
        prompt = f"""
You are verifying a product image candidate.

Verified product:
- Product name: {product_name}
- Brand: {brand}
- Barcode/UPC/EAN: {barcode}

Question:
Does this image appear to show the actual retail product/package for this verified product?

Return ONLY valid JSON:
{{
  "is_match": true,
  "confidence": "high|medium|low",
  "reason": "short explanation"
}}

Rules:
- Return false for logos, certification seals, trust badges, payment icons, banners, placeholders, or unrelated products.
- Return true only if the visible package/label appears to match the product name and brand.
"""

        response = self._client.models.generate_content(
            model=self._model_name,
            contents=[
                prompt,
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            ],
            config=self._extract_config,
        )

        raw = _collect_text(response)
        data = _extract_json(raw)

        return (
            bool(data.get("is_match"))
            and data.get("confidence") in {"high", "medium"}
        )

    except Exception as exc:
        log.warning("Gemini vision image verification failed safely: %s", exc)
        return False

def _get_page_text(url: str) -> tuple[bool, str, str, int]:
    """
    Fetch a candidate product URL and return:
      (reachable, final_url, visible-ish text/html, status_code)

    We prefer GET over HEAD because many retailer pages return 200 with a
    rendered error message. A URL is not considered valid merely because the
    HTTP status is 200.
    """
    try:
        resp = http_requests.get(
            url,
            allow_redirects=True,
            timeout=_URL_TIMEOUT,
            headers=_HTTP_HEADERS,
        )
        text = resp.text or ""
        return resp.status_code < 400, resp.url, text, resp.status_code
    except Exception as exc:
        log.debug("URL GET error (%s): %s", type(exc).__name__, url)
        return False, url, "", 0


def _page_is_not_found(text: str) -> bool:
    low = re.sub(r"\s+", " ", (text or "").lower())
    return any(marker in low for marker in _NOT_FOUND_MARKERS)


def _strip_html(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", text or "", flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_hkd_price(text: str) -> str | None:
    """
    Extract a plausible HKD price from retailer HTML/text. When both original
    and special prices appear, prefer the lowest plausible positive price.
    """
    plain = _strip_html(text)
    candidates: list[float] = []

    for m in re.finditer(r"(?:HK\$|HKD|\$)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", plain, flags=re.I):
        raw = m.group(1).replace(",", "")
        try:
            val = float(raw)
            if 1 <= val <= 10000:
                candidates.append(val)
        except ValueError:
            pass

    if not candidates:
        for m in re.finditer(r"(?:price|special price|售價|優惠價|價錢|價格)\s*[:：]?\s*([0-9][0-9,]*(?:\.\d{1,2})?)", plain, flags=re.I):
            raw = m.group(1).replace(",", "")
            try:
                val = float(raw)
                if 1 <= val <= 10000:
                    candidates.append(val)
            except ValueError:
                pass

    if not candidates:
        return None

    price = min(candidates)
    return f"HK${price:.2f}"


def _format_hkd_price(value) -> str | None:
    """Normalize LLM/page prices into the string format expected by RetailerListing."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        val = float(value)
        if 1 <= val <= 10000:
            return f"HK${val:.2f}"
        return None

    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "unknown", "—", "-"}:
        return None

    # Already contains a recognizable HKD marker; normalize the numeric part.
    m = re.search(r"(?:HK\$|HKD|\$)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text, flags=re.I)
    if m:
        try:
            return f"HK${float(m.group(1).replace(',', '')):.2f}"
        except ValueError:
            return text

    # LLM sometimes returns bare numbers, for example 18.0. Treat those as HKD
    # because this function is only called inside the HK retailer pipeline.
    m = re.fullmatch(r"[0-9][0-9,]*(?:\.\d{1,2})?", text)
    if m:
        try:
            return f"HK${float(text.replace(',', '')):.2f}"
        except ValueError:
            return None

    return text


def _coerce_retailer_item(item: dict) -> dict:
    """Make Gemini's retailer JSON safe for the Pydantic RetailerListing model."""
    item = dict(item or {})
    item["retailer_name"] = str(item.get("retailer_name") or "Unknown retailer").strip()
    item["url"] = str(item.get("url") or "").strip()
    item["price_hkd"] = _format_hkd_price(item.get("price_hkd"))
    if item.get("notes") is not None:
        item["notes"] = str(item.get("notes"))
    return item


def _candidate_url_repairs(url: str, product_name: str | None = None) -> list[str]:
    """
    Try generic URL repairs for common HK/Wix/Shopify product-page mistakes.

    Gemini sometimes returns a plausible but non-opening URL such as
    /products/<slug> even when the real store uses /product-page/<product-name>.
    This function creates a few deterministic alternatives without hardcoding a
    single barcode/product.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(u: str | None) -> None:
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return []
        hosts = [parsed.netloc]
        if parsed.netloc.startswith("www."):
            hosts.append(parsed.netloc[4:])
        else:
            hosts.append("www." + parsed.netloc)

        path = parsed.path or ""
        last_segment = path.rstrip("/").split("/")[-1]
        product_slug = quote((product_name or "").strip(), safe="-_.~") if product_name else ""

        for host in hosts:
            base = parsed._replace(netloc=host, query="", fragment="")
            if "/products/" in path:
                add(base._replace(path=path.replace("/products/", "/product-page/", 1)).geturl())
            if product_slug:
                add(base._replace(path=f"/product-page/{product_slug}").geturl())
            if last_segment:
                add(base._replace(path=f"/product-page/{last_segment}").geturl())
    except Exception:
        pass

    return out


_IMAGE_PLACEHOLDER_MARKERS = [
    "placeholder", "no-image", "no_image", "noimage", "blank", "transparent",
    "spacer", "loading", "loader", "spinner", "default", "dummy", "logo",
    "icon", "favicon", "avatar", "banner", "btn", "button", "sprite", "pixel",
    "seal", "seals", "certified", "certificate", "certification", "trust",
    "secure", "payment", "visa", "mastercard", "paypal", "badge",
    "legitscript", "trustpilot", "verified", "widget", "social",
]


def _looks_like_placeholder_image_url(url: str | None) -> bool:
    if not url:
        return True
    parsed = urlparse(url)
    path = parsed.path.lower()
    filename = path.rstrip("/").split("/")[-1]

    # Common generic names. These are frequently theme icons or placeholders.
    if filename in {"1.png", "0.png", "blank.png", "transparent.png", "loading.gif", "loader.gif", "spacer.gif"}:
        return True

    return any(marker in path for marker in _IMAGE_PLACEHOLDER_MARKERS)


def _image_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    """Return image dimensions from PNG/JPEG/GIF/WebP bytes without extra dependencies."""
    if not data or len(data) < 16:
        return None

    # PNG
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")

    # GIF
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")

    # JPEG
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            i += 2
            if marker in {0xD8, 0xD9}:
                continue
            if i + 2 > len(data):
                break
            seg_len = int.from_bytes(data[i:i + 2], "big")
            if seg_len < 2:
                break
            # SOF markers that contain dimensions
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if i + 7 <= len(data):
                    h = int.from_bytes(data[i + 3:i + 5], "big")
                    w = int.from_bytes(data[i + 5:i + 7], "big")
                    return w, h
                break
            i += seg_len

    # WebP VP8X
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X" and len(data) >= 30:
            w = 1 + int.from_bytes(data[24:27], "little")
            h = 1 + int.from_bytes(data[27:30], "little")
            return w, h

    return None


def _normalise_image_src(base_url: str, src: str | None) -> str | None:
    if not src:
        return None
    src = src.strip()
    if not src or src.startswith(("data:", "javascript:", "mailto:")):
        return None

    # srcset: choose the last candidate, usually the largest width.
    if "," in src and " " in src:
        parts = [p.strip().split(" ")[0] for p in src.split(",") if p.strip()]
        if parts:
            src = parts[-1]
    else:
        src = src.split(" ")[0]

    return _clean_public_source_url(urljoin(base_url, src))


def _image_candidate_score(url: str, context: str, product_name: str | None, brand: str | None) -> int:
    if not url or _looks_like_placeholder_image_url(url):
        return -1000

    path = _normalise_match_text(urlparse(url).path)
    ctx = _normalise_match_text(context or "")
    tokens = _identity_tokens(product_name, brand)

    score = 0
    if re.search(r"\.(jpg|jpeg|png|webp)(?:$|\?)", urlparse(url).path.lower()):
        score += 5
    if any(x in path for x in ["product", "products", "upload", "image", "images", "media", "catalog"]):
        score += 4
    for tok in tokens:
        if tok in path:
            score += 4
        if tok in ctx:
            score += 2
    if _host_matches(url, _HK_RETAILER_DOMAINS):
        score += 1
    return score


def _extract_image_candidates_from_html(base_url: str, html: str, product_name: str | None = None, brand: str | None = None) -> list[str]:
    """Extract likely product image URLs from meta tags, JSON-LD, and img tags."""
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()

    def add(src: str | None, context: str = "") -> None:
        u = _normalise_image_src(base_url, src)
        if not u or u in seen:
            return
        seen.add(u)
        candidates.append((_image_candidate_score(u, context, product_name, brand), u))

    # Meta tags: og:image, twitter:image, etc.
    for tag in re.findall(r"<meta\b[^>]*>", html or "", flags=re.I):
        low = tag.lower()
        if any(key in low for key in ["og:image", "twitter:image", "image_src"]):
            m = re.search(r"\bcontent=[\"']([^\"']+)[\"']", tag, flags=re.I)
            if m:
                add(m.group(1), tag)

    # JSON-LD or embedded JSON image fields.
    for m in re.finditer(r'"image"\s*:\s*("[^"]+"|\[[^\]]+\])', html or "", flags=re.I):
        raw = m.group(1)
        for u in re.findall(r'https?://[^"\]\s]+|/[^"\]\s]+\.(?:jpg|jpeg|png|webp)', raw, flags=re.I):
            add(u, raw)

    # IMG tags and lazy-loaded attributes.
    for tag in re.findall(r"<img\b[^>]*>", html or "", flags=re.I):
        attrs = dict(re.findall(r"([a-zA-Z0-9_:\-]+)\s*=\s*[\"']([^\"']+)[\"']", tag))
        context = " ".join([tag, attrs.get("alt", ""), attrs.get("title", ""), attrs.get("class", "")])
        for attr in ["data-zoom-image", "data-large_image", "data-large-image", "data-original", "data-src", "data-lazy", "srcset", "data-srcset", "src"]:
            add(attrs.get(attr), context)

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [u for score, u in candidates if score > -1000]


def _valid_direct_image_url(
    url: str | None,
    product_name: str | None = None,
    brand: str | None = None,
) -> str | None:
    """
    Validate that a direct image URL is likely a real product photo.
    The previous version accepted any image/* response, so tiny placeholders like
    /product_images/uploaded_images/1.png were saved as product images.
    """
    u = _clean_public_source_url(url)
    if not u:
        return None

    path = urlparse(u).path.lower()
    if not re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:$|[?])", path):
        return None
    if _looks_like_placeholder_image_url(u):
        return None
    if _image_negative_asset_penalty(u, product_name or "", brand or "") >= 100:
        return None

    try:
        resp = http_requests.get(u, headers=_HTTP_HEADERS, timeout=_URL_TIMEOUT, allow_redirects=True)
        ctype = resp.headers.get("content-type", "").lower()
        final_url = resp.url
        final_path = urlparse(final_url).path.lower()

        if resp.status_code >= 400:
            return None
        if "image/" not in ctype and not re.search(r"\.(?:jpg|jpeg|png|webp|gif)$", final_path):
            return None
        if _looks_like_placeholder_image_url(final_url):
            return None
        if _image_negative_asset_penalty(final_url, product_name or "", brand or "") >= 100:
            return None

        data = resp.content or b""
        dims = _image_dimensions_from_bytes(data)
        if dims:
            w, h = dims
            # Reject icons, paws, logos, tracking pixels, and other non-product art.
            if w < 180 or h < 180 or (w * h) < 40_000:
                return None
        elif len(data) < 8_000:
            # Unknown dimensions and very small file is usually not a product image.
            return None

        return final_url
    except Exception:
        return None


def _extract_best_image_from_page(
    page_url: str,
    product_name: str | None = None,
    brand: str | None = None,
) -> str | None:
    ok, final_url, html, status = _get_page_text(page_url)
    if not ok or not html:
        return None
    if _page_is_not_found(html):
        return None

    for candidate in _extract_image_candidates_from_html(final_url, html, product_name, brand):
        valid = _valid_direct_image_url(candidate, product_name, brand)
        if valid:
            return valid
    return None


def _resolve_product_image(
    prod_data: dict,
    retailers: list[RetailerListing],
    product_name: str | None,
    brand: str | None,
) -> str | None:
    """
    Resolve product image in priority order:
      1. Gemini/direct image_url if it is a real product-sized image.
      2. Manufacturer/evidence pages.
      3. Valid retailer product pages.
    """
    direct = _valid_direct_image_url(prod_data.get("image_url"), product_name, brand)
    if direct:
        return direct

    page_candidates: list[str] = []
    for key in ["manufacturer_url"]:
        u = _clean_public_source_url(prod_data.get(key))
        if u:
            page_candidates.append(u)

    for u in prod_data.get("evidence_urls") or []:
        u = _clean_public_source_url(u)
        if u:
            page_candidates.append(u)

    for r in retailers or []:
        u = _clean_public_source_url(getattr(r, "url", None))
        if u:
            page_candidates.append(u)

    seen: set[str] = set()
    for page_url in page_candidates:
        if page_url in seen:
            continue
        seen.add(page_url)
        image = _extract_best_image_from_page(page_url, product_name, brand)
        if image:
            return image

    return None


def _extract_candidate_urls_from_text(text: str) -> list[str]:
    """
    Pull direct URLs out of Gemini's grounded notes. Grounding metadata can be
    empty even when the generated notes contain URLs. This gives the retailer
    phase another deterministic source of candidates before asking the LLM to
    structure listings.
    """
    urls: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"https?://[^\s<>'\"）)\]]+", text or "", flags=re.I):
        raw = m.group(0).rstrip('.,;:!?。。，、)]}"')
        cleaned = _clean_public_source_url(raw)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)
    return urls


def _best_price_from_text(text: str) -> str | None:
    """
    Same idea as _extract_hkd_price(), but intended for search snippets/notes.
    Keeps the visible HKD price if the live retailer page cannot be fetched or
    the page is JS/geofence protected. Prefer the lowest visible current/special
    price when multiple HKD values appear.
    """
    return _extract_hkd_price(text or "")


def _search_snippet_valid_listing(
    url: str,
    context: str,
    barcode: str | None = None,
    product_name: str | None = None,
    brand: str | None = None,
) -> tuple[bool, str | None, bool | None]:
    """
    Fallback validator for retailer listings found in Google/Gemini snippets.

    Some HK retailer pages are JavaScript-heavy, temporarily block requests, or
    return a generic page to requests while the browser/Gemini search result is
    correct. In those cases we can still return a buy URL when the search snippet
    itself contains: product tokens + HK signal + a visible HKD price.

    This fallback is intentionally price-required and product-token-required so
    it does not turn into a hallucinated retailer result.
    """
    if not _looks_like_product_url(url):
        return False, None, None
    if _host_matches(url, _BLOCKED_SOURCE_DOMAINS):
        return False, None, None

    hay = f"{url}\n{context or ''}"
    if not _has_hk_signal(url, hay):
        return False, None, None

    plain = _normalise_match_text(hay)
    digits = re.sub(r"\D", "", barcode or "")
    barcode_hit = bool(digits and digits in re.sub(r"\D", "", plain))

    tokens = _identity_tokens(product_name, brand)
    token_hits = sum(1 for tok in tokens if tok in plain)
    token_ok = token_hits >= (2 if len(tokens) <= 3 else 3)

    if not (barcode_hit or token_ok):
        return False, None, None

    price = _best_price_from_text(hay)
    if not price:
        return False, None, None

    low = _strip_html(hay).lower()
    stock: bool | None = None
    if any(x in low for x in ["out of stock", "currently out of stock", "缺貨", "售罄", "notify me"]):
        stock = False
    elif any(x in low for x in ["add to cart", "加入購物車", "proceed to shop", "in stock"]):
        stock = True
    elif price:
        # Price exists, but snippet may not expose stock. Keep it unknown rather than wrong.
        stock = None

    return True, price, stock


def _context_for_url(facts: str, url: str, window: int = 1800) -> str:
    """Return nearby text around a URL in the grounded notes, falling back to all notes."""
    facts = facts or ""
    if not url:
        return facts[:window]
    idx = facts.find(url)
    if idx < 0:
        # Try matching by path tail because Gemini may shorten or normalize URLs.
        tail = urlparse(url).path.rstrip("/").split("/")[-1]
        idx = facts.find(tail) if tail else -1
    if idx < 0:
        return facts[:window]
    start = max(0, idx - window // 2)
    end = min(len(facts), idx + window // 2)
    return facts[start:end]

def _product_text_matches(
    url: str,
    text: str,
    barcode: str | None = None,
    product_name: str | None = None,
    brand: str | None = None,
) -> bool:
    """
    Generic product-page sanity check.

    A URL is accepted when:
      1. It is a plausible product page.
      2. It is not a not-found/error page.
      3. The page contains either the exact barcode or enough dynamic identity
         tokens from the verified product name/brand.

    This avoids barcode-specific keyword patches while still preventing wrong
    product pages from being cached.
    """
    if not _looks_like_product_url(url):
        return False
    if _page_is_not_found(text):
        return False

    plain = _normalise_match_text(_strip_html(text))
    digits = re.sub(r"\D", "", barcode or "")
    if digits and digits in re.sub(r"\D", "", plain):
        return True

    tokens = _identity_tokens(product_name, brand)
    if not tokens:
        return False

    hits = sum(1 for tok in tokens if tok in plain)
    # Short identities need stricter percentage; long identities need at least 3.
    required = 2 if len(tokens) <= 3 else 3
    return hits >= required


def _validate_product_page(
    url: str,
    barcode: str | None = None,
    product_name: str | None = None,
    brand: str | None = None,
) -> tuple[bool, str, str | None, bool | None]:
    """Validate the URL and try generic repaired alternatives before failing."""
    candidates = [url] + _candidate_url_repairs(url, product_name)
    seen: set[str] = set()
    last_status = 0

    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)

        ok, final_url, html, status = _get_page_text(candidate)
        last_status = status
        if not ok:
            continue
        if _page_is_not_found(html):
            continue
        if not _has_hk_signal(final_url, html):
            continue
        if not _product_text_matches(final_url, html, barcode, product_name, brand):
            continue

        price = _extract_hkd_price(html)
        plain = _strip_html(html).lower()
        in_stock: bool | None = None
        if any(x in plain for x in ["out of stock", "currently out of stock", "缺貨", "售罄"]):
            in_stock = False
        elif any(x in plain for x in ["add to cart", "加入購物車", "qty", "數量", "in stock", "立即購買"]):
            in_stock = True
        elif price:
            in_stock = True

        if candidate != url:
            log.info("  Repaired retailer URL: %s → %s", url, final_url)
        return True, final_url, price, in_stock

    log.info("  URL failed validation after repair attempts. Last HTTP status=%s: %s", last_status, url)
    return False, url, None, None






def _nutrition_has_values(nutrition: dict | None) -> bool:
    """
    Return True when at least one meaningful nutrition field exists.

    This prevents saving an empty nutrition panel when product identity is found
    but guaranteed analysis was not returned by the first identity search.
    """
    if not isinstance(nutrition, dict):
        return False

    keys = [
        "crude_protein_min",
        "crude_fat_min",
        "crude_fiber_max",
        "moisture_max",
        "ash_max",
        "calories",
    ]
    for key in keys:
        value = nutrition.get(key)
        if value is not None and str(value).strip() not in {"", "null", "none", "n/a", "—", "-"}:
            return True

    other = nutrition.get("other") or {}
    if isinstance(other, dict):
        for value in other.values():
            if value is not None and str(value).strip() not in {"", "null", "none", "n/a", "—", "-"}:
                return True

    return False

# ─── Prompts ─────────────────────────────────────────────────────────────────

_PRODUCT_SEARCH_PROMPT = """\
You are verifying the exact identity of a dog/cat food product by barcode.

Barcode to verify: {barcode}
Acceptable barcode variants only: {barcode_variants}

Search the web using the EXACT barcode string in quotes first. Then search the
exact barcode with "pet food", "cat food", "dog food", "guaranteed analysis",
"nutrition", "ingredients", "SKU", and "自訂編碼".

Prioritise product-detail pages, manufacturer/brand pages, retailer pages,
barcode/SKU pages, product-label pages, and pages that explicitly show the
barcode/EAN/UPC/SKU together with the product name. Do not limit discovery to
any one brand or any fixed retailer list.

Important rules:
- Do NOT guess the product from brand, flavour, or similar search results.
- Do NOT use a product unless a source explicitly connects one acceptable
  barcode variant to that exact product.
- Product package size/flavour/animal must match the barcode evidence.
- Prefer manufacturer, official brand, retailer product page, or product label
  evidence.
- Include any conflicting candidates you see, but clearly separate low-trust
  foreign/aggregator conflicts from stronger HK retailer or brand evidence.

Return factual research notes with URLs and exact values where available.
"""

_PRODUCT_EXTRACT_PROMPT = """\
Extract product identity from the research notes below.

Return ONLY valid JSON. No markdown. No prose. No trailing commas.
Use null/true/false lowercase.

Barcode to verify: {barcode}
Acceptable variants: {barcode_variants}

Strict verification rules:
- "barcode_verified" must be true ONLY when the notes explicitly connect one
  acceptable barcode variant to the product.
- If the notes only show a similar product, same brand, same flavour, or a
  barcode from the prompt/query but not a source, set barcode_verified=false.
- If barcode_verified=false, set product_name=null, brand=null, and
  identity_confidence="low".
- If two or more trusted HK/brand sources explicitly connect the same acceptable
  barcode variant to the same product, barcode_verified can be true even if one
  unrelated foreign/aggregator result conflicts. Put that conflict in warnings.
- identity_confidence:
  high   = exact barcode + product name + at least one strong source URL
  medium = exact barcode + product name but source is retailer/snippet only
  low    = no exact barcode evidence or conflicting product evidence
- Put one short exact evidence sentence/snippet in "barcode_evidence" (max 300 characters).
- Put at most 5 public source URLs used for identity/nutrition in "evidence_urls".
- Do NOT include Google, Vertex AI, search-result, cached, or redirect URLs in evidence_urls.
- Keep "warnings" short. Include conflicts or uncertainty only.

Research notes:
{facts}

JSON structure:
{{
  "barcode_verified": false,
  "identity_confidence": "low",
  "barcode_evidence": null,
  "evidence_urls": [],
  "warnings": [],
  "product_name": null,
  "brand": null,
  "target_animal": null,
  "manufacturer_url": null,
  "image_url": null,
  "nutritional_info": {{
    "crude_protein_min": null,
    "crude_fat_min": null,
    "crude_fiber_max": null,
    "moisture_max": null,
    "ash_max": null,
    "calories": null,
    "other": {{}}
  }}
}}
"""

_RETAILER_SEARCH_PROMPT = """\
Search Hong Kong online pet-food retailers for this EXACT VERIFIED product.

Barcode : {barcode}
Variants: {barcode_variants}
Product : {product_name}
Brand   : {brand}

IMPORTANT DESIGN RULE:
- Known store names/domains are search HINTS only. Do NOT limit results to a
  fixed domain list. Millions of stores can exist; accept any Hong Kong online
  store if it has evidence.
- Every candidate MUST include the raw, full, direct product page URL.
- Do not summarize a listing unless you can provide its actual URL and visible
  HKD price.

Search broadly using the verified product identity, for example:
- "{barcode}" "HK$"
- "{barcode}" "HKD"
- "{barcode}" "香港"
- "{barcode}" "Hong Kong"
- "{product_name}" "HK$"
- "{product_name}" "HKD"
- "{product_name}" "香港"
- "{product_name}" "加入購物車"
- "{brand}" "{product_name}" "price"
- "{brand}" "{product_name}" "Hong Kong pet shop"

Use these as helpful search hints only, not restrictions:
HKTVmall, PetPetHome, Vetopia, PetMarket, Q-Pets, Petko, A-Mart,
Whiskers N Paws, PetStation, Pawsmore, PetBoo, Paws United, ePet,
My Pet Store, Forever Pets, PetPetGroup.

HK pricing/buy URL rules:
- Pricing and buy URL search is Hong Kong only.
- A valid listing must have evidence of HK location/currency, e.g. .hk/.com.hk,
  HK$, HKD, 香港, 港幣, Hong Kong, or Hong Kong delivery/cart language.
- Return only direct product pages, not search/category/homepage URLs.
- A visible HKD price is required. Use the current/special price when visible.
- Out-of-stock product pages are acceptable only if a visible HKD price is shown;
  mark in_stock=false.
- Product/flavour/animal/size must match the verified product. Multipacks are OK
  only when the base unit is the same product.
- Do not invent URLs or prices.

Return factual notes. For each listing include:
1. retailer name
2. full direct product URL
3. visible HKD price
4. stock status
5. pack size / multipack notes
"""

_PRICE_MATCH_PROMPT = """\
Below are:
A) Research notes about Hong Kong retailer listings
B) Real URLs returned by Google Search grounding

Match only URLs that are direct product pages for the exact verified product:
{product_name}
Barcode variants: {barcode_variants}

Return ONLY valid JSON. No markdown. No prose. No trailing commas.
Use null/true/false lowercase.

Research notes:
{facts}

Grounded URLs:
{url_list}

Rules:
- Use URLs exactly as provided.
- Drop search pages, category pages, homepages, error pages, not-found pages, and wrong product URLs.
- Extract the visible current/special price in HKD from the page/snippet.
- If price is not found, set price_hkd=null.
- in_stock=true only if available/price/current listing is shown.
- in_stock=false only if explicitly out of stock.

JSON:
{{
  "hk_retailers": [
    {{
      "retailer_name": "<store name>",
      "url": "<exact URL>",
      "price_hkd": null,
      "in_stock": null,
      "notes": "<pack size/promo/evidence note>"
    }}
  ]
}}
"""

_FALLBACK_RETAILER_EXTRACT_PROMPT = """\
Extract Hong Kong retailer listings from the notes below.

Return ONLY valid JSON. No markdown. No prose. No trailing commas.
Use null/true/false lowercase.

Exact verified product: {product_name}
Barcode variants: {barcode_variants}

Rules:
- Do not rely on a fixed domain list. Any Hong Kong online retailer can qualify.
- Only include direct product-page URLs, not search/category/homepage URLs.
- Include only listings with HK evidence: HK$, HKD, 港幣, 香港, Hong Kong,
  .hk/.com.hk domain, or Hong Kong delivery/cart language.
- Always include a visible HKD price if the notes/snippet show one.
- Do not invent URLs or prices; omit listings that lack a direct URL.

Notes:
{facts}

JSON:
{{
  "hk_retailers": [
    {{
      "retailer_name": "<store name>",
      "url": "<confirmed direct product page URL>",
      "price_hkd": null,
      "in_stock": null,
      "notes": ""
    }}
  ]
}}
"""


_GLOBAL_NUTRITION_SEARCH_PROMPT = """\
Search globally for the guaranteed/nutritional analysis for this exact verified pet food product.

Barcode : {barcode}
Variants: {barcode_variants}
Product : {product_name}
Brand   : {brand}

This nutrition search is GLOBAL and must NOT be restricted to Hong Kong. Good sources include:
- official brand/manufacturer product pages
- distributor/catalog pages
- retailer product pages with label text
- product packaging/label pages
- PDF/catalog/specification pages

Rules:
- Return nutrition only for the same product identity, animal, flavour/recipe, and pack/can/bag size where possible.
- If barcode evidence is unavailable on the nutrition page, the product name/brand/flavour/size must strongly match the verified product.
- Look specifically for guaranteed analysis / analytical constituents / typical analysis / 營養分析 / 保證分析 / 成分分析.
- Extract exact values with units where shown: crude protein, crude fat, crude fiber/fibre, moisture, ash, calories/energy, calcium, phosphorus, sodium, etc.
- Do not invent missing values.
- Include source URLs where the values were found.

Return factual notes with exact nutrient values and source URLs.
"""

_NUTRITION_EXTRACT_PROMPT = """\
Extract guaranteed/nutritional analysis from these global nutrition search notes.

Return ONLY valid JSON. No markdown. No prose. No trailing commas.
Use null/true/false lowercase.

Verified product:
Barcode : {barcode}
Product : {product_name}
Brand   : {brand}

Rules:
- Use only values that appear in the notes or source snippets.
- Do not invent values.
- Values should be strings with units when possible, e.g. "28%", "10%", "85 kcal/100g".
- If a value is numeric only, keep it as a string, e.g. "28".
- If no nutrition values are found, return all fields as null.

Search notes:
{facts}

JSON:
{{
  "nutritional_info": {{
    "crude_protein_min": null,
    "crude_fat_min": null,
    "crude_fiber_max": null,
    "moisture_max": null,
    "ash_max": null,
    "calories": null,
    "other": {{}}
  }},
  "source_urls": [],
  "confidence": "low",
  "warnings": []
}}
"""


_GLOBAL_IMAGE_SEARCH_PROMPT = """\
Search globally for product images for this exact verified pet food product.

Barcode : {barcode}
Variants: {barcode_variants}
Product : {product_name}
Brand   : {brand}

Goal:
Find the best real product pack/can/bag image. This image search is GLOBAL and
must NOT be restricted to Hong Kong. Good sources include official brand pages,
manufacturer pages, distributors, catalog pages, and retailer product pages in
any country.

Rules:
- Return only images for the same product identity, flavour/recipe, animal, and pack/can/bag size where possible.
- Prefer direct image URLs ending in .jpg, .jpeg, .png, or .webp.
- Also include the product page URL where the image was found.
- Do NOT return logos, icons, placeholder images, loading images, category images, banners, or generic brand images.
- Do NOT use price or retailer location rules here; those apply only to HK retailer pricing.
- If several images are available, rank official/brand/manufacturer first, then distributor/catalog, then retailer images.

Return factual notes with candidate direct image URLs and source page URLs.
"""

_IMAGE_CANDIDATE_EXTRACT_PROMPT = """\
Extract product image candidates from these global search notes.

Return ONLY valid JSON. No markdown. No prose. No trailing commas.
Use null/true/false lowercase.

Verified product:
Barcode : {barcode}
Product : {product_name}
Brand   : {brand}

Rules:
- Image search is global. Do not restrict to Hong Kong.
- Candidate image must match the verified product, not just the brand.
- Do not include logos, icons, placeholders, spinners, generic banners, or category images.
- Prefer direct image URLs. If only a source product page is available, set image_url=null and source_url to that page.
- confidence must be high, medium, or low.

Search notes:
{facts}

JSON:
{{
  "image_candidates": [
    {{
      "image_url": "<direct jpg/png/webp URL or null>",
      "source_url": "<page where image was found or null>",
      "source_type": "manufacturer|brand|distributor|retailer|catalog|other",
      "confidence": "high|medium|low",
      "reason": "short reason why this image matches"
    }}
  ]
}}
"""


_GLOBAL_IMAGE_URL_RECOVERY_PROMPT = """\
Find candidate product-image source URLs for this exact verified pet-food product.

Barcode : {barcode}
Variants: {barcode_variants}
Product : {product_name}
Brand   : {brand}

This is GLOBAL image discovery. Do NOT restrict to Hong Kong.
Use the verified product name/brand/barcode to search the web like a normal
Google product-image search.

Rules:
- Return raw URLs only when they are real public URLs.
- Candidate can be a direct image URL or a product/source page URL that contains
  the image.
- Accept manufacturer, brand, distributor, catalog, or retailer pages in any country.
- Do not return logos, placeholders, icons, category pages, or generic brand pages.
- Do not invent URLs.

Return ONLY JSON:
{{
  "image_candidates": [
    {{
      "image_url": "<direct image URL or null>",
      "source_url": "<source/product page URL or null>",
      "confidence": "high|medium|low",
      "reason": "short evidence"
    }}
  ]
}}
"""

_HK_RETAILER_URL_RECOVERY_PROMPT = """\
Find direct Hong Kong buy-page URLs for this exact verified pet-food product.

Barcode : {barcode}
Variants: {barcode_variants}
Product : {product_name}
Brand   : {brand}

Search like a user looking for where to buy this exact product in Hong Kong.
Do NOT restrict yourself to a fixed domain list. Known HK stores can be used as
hints, but any HK online retailer may qualify.

Use queries such as:
{queries}

Rules:
- Return only raw direct product-page URLs.
- HK pricing/buy URL only: page/snippet must show HK$, HKD, 香港, 港幣,
  Hong Kong, .hk/.com.hk, or Hong Kong delivery/cart evidence.
- The listing must match the verified product name/brand/flavour/animal/size.
- A visible HKD price is required if known.
- Do not return search pages, category pages, homepages, or global/non-HK stores.
- Do not invent URLs.

Return ONLY JSON:
{{
  "hk_retailers": [
    {{
      "retailer_name": "<store name or null>",
      "url": "https://...",
      "price_hkd": null,
      "in_stock": null,
      "notes": "short evidence/snippet"
    }}
  ],
  "urls": ["https://..."]
}}
"""


# ─── Searcher class ───────────────────────────────────────────────────────────

class ProductSearcher:
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

        self._search_config = types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=4096,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        )
        self._extract_config = types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=8192,
            response_mime_type="application/json",
        )

        log.info(
            "Vertex AI ready | project=%s location=%s model=%s search_grounding=ON",
            cfg.google_cloud_project, cfg.google_cloud_location, self._model_name,
        )

    def _grounded_search(self, prompt: str) -> tuple[str, list[str], list[str]]:
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=self._search_config,
        )
        text = _collect_text(response)
        urls = _extract_grounding_urls(response)
        queries = _grounding_search_queries(response)

        log.info(
            "Grounded search: %d chars | %d source URLs | queries=%s",
            len(text), len(urls), queries[:6],
        )
        if urls:
            log.debug("All grounding URLs:\n  %s", "\n  ".join(urls))
        return text, urls, queries

    def _extract_product_json(self, barcode: str, facts: str, grounding_urls: list[str]) -> dict:
        public_urls = _clean_url_list(grounding_urls)
        url_context = ""
        if public_urls:
            url_context = "\n\nPublic grounding URLs only:\n" + "\n".join(f"- {u}" for u in public_urls)

        prompt = _PRODUCT_EXTRACT_PROMPT.format(
            barcode=barcode,
            barcode_variants=_quoted_variants(barcode),
            facts=(facts or "")[:12000] + url_context,
        )

        response = self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=self._extract_config,
        )
        raw = _collect_text(response)

        try:
            data = _extract_json(raw)
        except Exception as exc:
            # Do not crash the CLI or cache a guessed product when JSON output is malformed.
            log.warning(
                "Product JSON extraction failed safely: %s\nRaw extraction output: %s",
                exc,
                raw[:1200],
            )
            return {
                "barcode_verified": False,
                "identity_confidence": "low",
                "barcode_evidence": None,
                "evidence_urls": public_urls,
                "warnings": [
                    "Gemini returned malformed/truncated JSON during product extraction; result was not cached."
                ],
                "product_name": None,
                "brand": None,
                "target_animal": None,
                "manufacturer_url": None,
                "image_url": None,
                "nutritional_info": {
                    "crude_protein_min": None,
                    "crude_fat_min": None,
                    "crude_fiber_max": None,
                    "moisture_max": None,
                    "ash_max": None,
                    "calories": None,
                    "other": {},
                },
            }

        # Safety normalization: never allow a non-verified product name through.
        if not data.get("barcode_verified"):
            data["barcode_verified"] = False
            data["identity_confidence"] = "low"
            data["product_name"] = None
            data["brand"] = None

        if data.get("identity_confidence") not in {"high", "medium", "low"}:
            data["identity_confidence"] = "low"

        # Keep evidence URLs public and short. If the model emitted only internal
        # Vertex/Google links, replace them with the public grounding URLs.
        model_urls = data.get("evidence_urls") or []
        if not isinstance(model_urls, list):
            model_urls = []
        cleaned_evidence_urls = _clean_url_list(model_urls)
        data["evidence_urls"] = cleaned_evidence_urls or public_urls

        if data.get("barcode_evidence") and len(str(data["barcode_evidence"])) > 500:
            data["barcode_evidence"] = str(data["barcode_evidence"])[:500] + "…"

        warnings = data.get("warnings") or []
        if isinstance(warnings, str):
            warnings = [warnings]
        data["warnings"] = [str(w)[:300] for w in warnings[:5]]

        return data

    @staticmethod
    def _is_verified(data: dict) -> bool:
        return (
            bool(data.get("barcode_verified"))
            and data.get("identity_confidence") in {"high", "medium"}
            and bool(data.get("product_name"))
        )

    def _fetch_product_info(
        self,
        barcode: str,
        serpapi_budget: SerpApiBudget | None = None,
    ) -> tuple[dict, str]:
        log.info("Phase 1 – exact product identity verification (barcode=%s) …", barcode)

        facts_1, urls_1, _ = self._grounded_search(
            _PRODUCT_SEARCH_PROMPT.format(
                barcode=barcode,
                barcode_variants=_quoted_variants(barcode),
            )
        )
        data = self._extract_product_json(barcode, facts_1, urls_1)
        if self._is_verified(data):
            return data, facts_1

        log.warning(
            "Exact barcode identity not verified on first pass. "
            "Trying one rescue search before returning Unknown Product."
        )

        rescue_queries: list[str] = []
        for v in _barcode_variants(barcode):
            rescue_queries.extend([
                f'"{v}"',
                f'"{v}" "barcode"',
                f'"{v}" "EAN"',
                f'"{v}" "UPC"',
                f'"{v}" "SKU"',
                f'"{v}" "自訂編碼"',
                f'"{v}" "pet food"',
                f'"{v}" "cat food"',
                f'"{v}" "dog food"',
                f'"{v}" "ingredients"',
                f'"{v}" "nutrition"',
                f'"{v}" "guaranteed analysis"',
            ])

        rescue_prompt = (
            _PRODUCT_SEARCH_PROMPT.format(
                barcode=barcode,
                barcode_variants=_quoted_variants(barcode),
            )
            + "\n\nRescue search: also try these exact queries:\n"
            + "\n".join(rescue_queries)
        )

        facts_2, urls_2, _ = self._grounded_search(rescue_prompt)
        merged_facts = facts_1 + "\n\n--- RESCUE SEARCH ---\n\n" + facts_2
        merged_urls = list(dict.fromkeys(urls_1 + urls_2))
        data = self._extract_product_json(barcode, merged_facts, merged_urls)

        if self._is_verified(data):
            return data, merged_facts

        # ── SerpAPI Google AI Mode identity rescue ────────────────────────────
        log.warning(
            "Vertex AI grounding could not verify barcode %s. "
            "Trying SerpAPI Google AI Mode identity rescue …",
            barcode,
        )
        ai_facts, ai_urls, search_id = _serpapi_google_ai_mode_identity(
            barcode,
            budget=serpapi_budget,
        )
        if ai_facts or ai_urls:
            ai_section = ai_facts
            if search_id:
                ai_section = f"SerpAPI Google AI Mode search_id={search_id}\n\n{ai_facts}"
            merged_facts = (
                merged_facts
                + "\n\n--- SERPAPI GOOGLE AI MODE ---\n\n"
                + ai_section
            )
            merged_urls = list(dict.fromkeys(merged_urls + ai_urls))
            data = self._extract_product_json(barcode, merged_facts, merged_urls)

        if not self._is_verified(data):
            log.warning(
                "SerpAPI AI Mode could not verify barcode %s. "
                "Trying SerpAPI Google Search barcode fallback …",
                barcode,
            )
            search_facts, search_urls, search_ids = _serpapi_google_barcode_search_identity(
                barcode,
                budget=serpapi_budget,
            )
            if search_facts or search_urls:
                search_section = search_facts
                if search_ids:
                    search_section = (
                        f"SerpAPI Google Search search_ids={search_ids}\n\n{search_facts}"
                    )
                merged_facts = (
                    merged_facts
                    + "\n\n--- SERPAPI GOOGLE BARCODE SEARCH ---\n\n"
                    + search_section
                )
                merged_urls = list(dict.fromkeys(merged_urls + search_urls))
                data = self._extract_product_json(barcode, merged_facts, merged_urls)

        if not self._is_verified(data):
            warnings = data.get("warnings") or []
            warnings.append(
                "Barcode identity was not verified from source evidence; result was not saved to cache/vector store."
            )
            data["warnings"] = warnings

        return data, merged_facts

    def _match_prices_to_urls(
        self,
        barcode: str,
        product_name: str,
        facts: str,
        hk_urls: list[str],
    ) -> list[dict]:
        url_list = "\n".join(f"- {u}" for u in hk_urls)
        prompt = _PRICE_MATCH_PROMPT.format(
            product_name=product_name,
            barcode_variants=_quoted_variants(barcode),
            facts=facts,
            url_list=url_list,
        )
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=self._extract_config,
        )
        raw = _collect_text(response)
        try:
            data = _extract_json(raw)
            return data.get("hk_retailers", []) or []
        except Exception as exc:
            log.warning("Price matching JSON extraction failed safely: %s\nRaw: %s", exc, raw[:800])
            return []

    def _extract_retailers_from_text(
        self,
        barcode: str,
        product_name: str,
        facts: str,
    ) -> list[dict]:
        prompt = _FALLBACK_RETAILER_EXTRACT_PROMPT.format(
            product_name=product_name,
            barcode_variants=_quoted_variants(barcode),
            facts=facts,
        )
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=self._extract_config,
        )
        raw = _collect_text(response)
        try:
            data = _extract_json(raw)
            return data.get("hk_retailers", []) or []
        except Exception as exc:
            log.warning("Fallback retailer extraction failed: %s\nRaw: %s", exc, raw[:400])
            return []

    def _validate_retailers(
        self,
        raw_listings: list[dict],
        barcode: str,
        product_name: str,
        brand: str,
        facts: str = "",
    ) -> list[RetailerListing]:
        validated: list[RetailerListing] = []
        seen_urls: set[str] = set()

        for item in raw_listings:
            item = _coerce_retailer_item(item)
            url = (item.get("url") or "").strip()
            if not url or url.lower() in {"null", "none"}:
                continue
            if url in seen_urls:
                continue
            if _host_matches(url, _BLOCKED_SOURCE_DOMAINS):
                log.info("  Dropping blocked/internal URL: %s", url)
                continue
            if not _looks_like_product_url(url):
                log.info("  Dropping non-product URL: %s", url)
                continue

            log.info("  Checking retailer URL: %s", url)
            valid_page, final_url, page_price, page_stock = _validate_product_page(url, barcode, product_name, brand)

            # Primary path: live page opened and matched.
            if valid_page:
                item["url"] = final_url
                if not item.get("price_hkd") and page_price:
                    item["price_hkd"] = page_price
                if item.get("in_stock") is None and page_stock is not None:
                    item["in_stock"] = page_stock
            else:
                # Verifiable buy URLs must open and validate in Python.
                # Do not accept snippet-only URLs as final buy links; this prevents
                # broken/blocked URLs from appearing as purchase sources.
                log.info("  Dropping retailer URL because live page validation failed: %s", url)
                continue

            item["price_hkd"] = _format_hkd_price(item.get("price_hkd"))
            if not item.get("price_hkd"):
                log.info("  Dropping URL because no HKD price was found: %s", item.get("url") or url)
                continue

            try:
                listing = RetailerListing(**item)
                validated.append(listing)
                seen_urls.add(final_url)
                log.info("  ✔ %s  %s", item.get("retailer_name"), item.get("price_hkd"))
            except Exception as exc:
                log.warning("  Malformed retailer entry: %s – %s", item, exc)

            if len(validated) >= _MAX_RETAILERS:
                break

        return validated


    def _extract_nutrition_json(
        self,
        barcode: str,
        product_name: str,
        brand: str,
        facts: str,
    ) -> dict:
        """Structure global nutrition-search notes into NutritionalInfo-like JSON."""
        prompt = _NUTRITION_EXTRACT_PROMPT.format(
            barcode=barcode,
            product_name=product_name,
            brand=brand or "",
            facts=(facts or "")[:12000],
        )
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=self._extract_config,
        )
        raw = _collect_text(response)
        try:
            data = _extract_json(raw)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            log.warning("Nutrition extraction failed safely: %s\nRaw: %s", exc, raw[:800])
            return {}

    def _fetch_global_nutrition(
        self,
        barcode: str,
        product_name: str,
        brand: str,
    ) -> dict:
        """
        Global nutrition discovery.

        This is intentionally separate from product identity and image search.
        It runs only when Phase 1 identity extraction verifies the product but
        returns empty nutrition fields.
        """
        log.info("Phase 1C – global nutrition discovery …")
        facts, grounded_urls, _ = self._grounded_search(
            _GLOBAL_NUTRITION_SEARCH_PROMPT.format(
                barcode=barcode,
                barcode_variants=_quoted_variants(barcode),
                product_name=product_name,
                brand=brand or "",
            )
        )

        data = self._extract_nutrition_json(barcode, product_name, brand, facts)
        nutrition = data.get("nutritional_info") or {}

        if _nutrition_has_values(nutrition):
            log.info("Global nutrition values found for barcode=%s", barcode)
            return nutrition

        log.warning("No global nutrition values found for barcode=%s", barcode)
        return {}


    def _extract_image_candidates_json(
        self,
        barcode: str,
        product_name: str,
        brand: str,
        facts: str,
    ) -> list[dict]:
        """Structure global image-search notes into candidate image/page URLs."""
        prompt = _IMAGE_CANDIDATE_EXTRACT_PROMPT.format(
            barcode=barcode,
            product_name=product_name,
            brand=brand or "",
            facts=(facts or "")[:12000],
        )
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
            config=self._extract_config,
        )
        raw = _collect_text(response)
        try:
            data = _extract_json(raw)
            items = data.get("image_candidates", []) or []
            return items if isinstance(items, list) else []
        except Exception as exc:
            log.warning("Global image candidate extraction failed safely: %s\nRaw: %s", exc, raw[:800])
            return []


    def _recover_global_image_candidates(
        self,
        barcode: str,
        product_name: str,
        brand: str,
    ) -> tuple[list[dict], str, list[str]]:
        """
        Second-pass global image URL recovery.

        This handles the common Gemini-grounding case where the first image
        search returns useful text but zero exposed grounding URLs. The LLM is
        asked specifically for raw image/source URLs, but Python still validates
        every candidate before accepting it.
        """
        facts, urls, _ = self._grounded_search(
            _GLOBAL_IMAGE_URL_RECOVERY_PROMPT.format(
                barcode=barcode,
                barcode_variants=_quoted_variants(barcode),
                product_name=product_name,
                brand=brand or "",
            )
        )

        candidates: list[dict] = []
        try:
            data = _extract_json(facts)
            raw_items = data.get("image_candidates", []) or []
            if isinstance(raw_items, list):
                candidates.extend([x for x in raw_items if isinstance(x, dict)])
        except Exception:
            # The recovery response may be prose even with JSON instructions.
            pass

        recovered_urls = list(dict.fromkeys(
            _clean_url_list(urls, limit=25) + _extract_candidate_urls_from_text(facts)
        ))
        for u in recovered_urls:
            path = urlparse(u).path.lower()
            if re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:$|[?])", path):
                candidates.append({
                    "image_url": u,
                    "source_url": None,
                    "confidence": "medium",
                    "reason": "Recovered direct image URL from global image search.",
                })
            else:
                candidates.append({
                    "image_url": None,
                    "source_url": u,
                    "confidence": "medium",
                    "reason": "Recovered source page URL from global image search.",
                })

        return candidates, facts, recovered_urls

    def _recover_hk_retailer_candidates(
        self,
        barcode: str,
        product_name: str,
        brand: str,
    ) -> tuple[list[dict], str, list[str]]:
        """
        Second-pass HK retailer URL recovery.

        This is intentionally evidence-based, not domain-list based. The LLM can
        use known stores as search hints, but any URL must still pass Python's
        HK-signal + product-match + HKD-price validation before display/cache.
        """
        recovery_queries: list[str] = []
        for v in _barcode_variants(barcode):
            recovery_queries.extend([
                f'"{v}" "HK$"',
                f'"{v}" "HKD"',
                f'"{v}" "香港"',
                f'"{v}" "Hong Kong"',
                f'"{v}" "加入購物車"',
            ])
        recovery_queries.extend([
            f'"{product_name}" "HK$"',
            f'"{product_name}" "HKD"',
            f'"{product_name}" "香港"',
            f'"{product_name}" "Hong Kong pet shop"',
            f'"{brand}" "{product_name}" "HK$"',
        ])

        facts, urls, _ = self._grounded_search(
            _HK_RETAILER_URL_RECOVERY_PROMPT.format(
                barcode=barcode,
                barcode_variants=_quoted_variants(barcode),
                product_name=product_name,
                brand=brand or "",
                queries="\n".join(f"- {q}" for q in recovery_queries),
            )
        )

        listings: list[dict] = []
        recovered_urls: list[str] = []
        try:
            data = _extract_json(facts)
            raw_listings = data.get("hk_retailers", []) or []
            if isinstance(raw_listings, list):
                listings.extend([x for x in raw_listings if isinstance(x, dict)])
            raw_urls = data.get("urls", []) or []
            if isinstance(raw_urls, list):
                recovered_urls.extend([str(u) for u in raw_urls])
        except Exception:
            pass

        recovered_urls = list(dict.fromkeys(
            _clean_url_list(urls, limit=25)
            + _clean_url_list(recovered_urls, limit=25)
            + _extract_candidate_urls_from_text(facts)
        ))
        for u in recovered_urls:
            if _looks_like_product_url(u) and not _host_matches(u, _BLOCKED_SOURCE_DOMAINS):
                listings.append({
                    "retailer_name": urlparse(u).netloc.lower().lstrip("www."),
                    "url": u,
                    "price_hkd": None,
                    "in_stock": None,
                    "notes": "Recovered candidate URL from HK retailer URL search.",
                })

        return listings, facts, recovered_urls

    def _vision_verify_product_image(
        self,
        image_bytes: bytes,
        mime_type: str,
        product_name: str,
        brand: str,
        barcode: str,
    ) -> bool:
        """
        Ask Gemini Vision whether the candidate image appears to be the correct product.
        This verifies candidates discovered by SerpAPI / page extraction. It does not
        perform image search by itself.
        """
        try:
            prompt = f"""
You are verifying a product image candidate.

Verified product:
- Product name: {product_name}
- Brand: {brand}
- Barcode/UPC/EAN: {barcode}

Does this image show the actual retail product/package for this verified product?

Return ONLY valid JSON:
{{
  "is_match": true,
  "confidence": "high|medium|low",
  "reason": "short explanation"
}}

Rules:
- Return false for logos, certification seals, trust badges, payment icons, banners, placeholders, or unrelated products.
- Return true only if the visible package/label appears to match the product name and brand.
"""
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=[
                    prompt,
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type or "image/jpeg"),
                ],
                config=self._extract_config,
            )
            raw = _collect_text(response)
            data = _extract_json(raw)
            return bool(data.get("is_match")) and data.get("confidence") in {"high", "medium"}
        except Exception as exc:
            log.warning("Gemini vision image verification failed safely: %s", exc)
            return False

    def _fetch_reliable_product_image_url(
        self,
        barcode: str,
        product_name: str,
        brand: str,
        serpapi_budget: SerpApiBudget | None = None,
    ) -> str | None:
        """
        Reliable GLOBAL product image discovery using SerpAPI Google Images first.

        Selection order:
          1. Discover candidates with SerpAPI Google Images.
          2. Validate each image with Python: image/* response, dimensions, product-token
             evidence, and non-product asset penalties.
          3. Prefer official/brand/manufacturer-looking candidates when available.
          4. Gemini Vision checks only the top N candidates to control cost.
          5. If Vision fails or is unavailable, use the best Python-validated candidate.

        Important:
          - This is GLOBAL image search, not HK-only.
          - The image does not need to come from a Hong Kong retailer.
          - If a verified brand/manufacturer image exists, it should win over retailer/CDN
            images such as BigCommerce/CDN product copies.
        """
        log.info("Phase 1B – SerpAPI global product image search …")
        candidates = _serpapi_google_image_candidates(
            product_name=product_name,
            brand=brand,
            barcode=barcode,
            max_results=_env_int("SERPAPI_IMAGE_MAX_CANDIDATES", 20),
            budget=serpapi_budget,
        )
        if not candidates:
            log.info("No SerpAPI image candidates found; falling back to Gemini grounding image discovery.")
            return None

        official_ranked: list[tuple[int, str, bytes, str]] = []
        other_ranked: list[tuple[int, str, bytes, str]] = []

        for candidate in candidates:
            ok, final_url, image_bytes, mime_type, score = _is_candidate_image_valid(
                candidate=candidate,
                product_name=product_name,
                brand=brand,
                barcode=barcode,
            )
            if not ok or not final_url or not image_bytes or not mime_type:
                continue

            source_url = str(candidate.get("source_url") or "")
            title = str(candidate.get("title") or "")

            if _is_official_brand_image_candidate(candidate, final_url, product_name, brand):
                # Large bonus forces verified official/brand sources to win over retailer CDNs.
                official_score = score + _candidate_brand_source_bonus(candidate, final_url, brand) + 1000
                log.info(
                    "Official/brand image candidate score=%s url=%s source=%s title=%s",
                    official_score,
                    final_url,
                    source_url,
                    title[:120],
                )
                official_ranked.append((official_score, final_url, image_bytes, mime_type))
            else:
                log.info(
                    "Retailer/catalog image candidate score=%s url=%s source=%s title=%s",
                    score,
                    final_url,
                    source_url,
                    title[:120],
                )
                other_ranked.append((score, final_url, image_bytes, mime_type))

        official_ranked.sort(key=lambda x: x[0], reverse=True)
        other_ranked.sort(key=lambda x: x[0], reverse=True)
        ranked = official_ranked + other_ranked

        if not ranked:
            log.warning("No SerpAPI image candidates passed Python validation.")
            return None

        # Verify only top few with Gemini Vision to control cost.
        # Because official candidates are now ranked first, Vision sees the best source first.
        vision_limit = _env_int("SERPAPI_VISION_MAX_CANDIDATES", 1)
        for score, image_url, image_bytes, mime_type in ranked[:vision_limit]:
            if self._vision_verify_product_image(image_bytes, mime_type, product_name, brand, barcode):
                log.info("Selected Gemini-verified SerpAPI product image: %s score=%s", image_url, score)
                return image_url

        # Safe fallback: top Python-validated image.
        # If an official/brand image passed validation, it will be ranked before retailer/CDN images.
        best_score, best_url, _best_bytes, _best_mime = ranked[0]
        log.info("Selected Python-validated SerpAPI product image: %s score=%s", best_url, best_score)
        return best_url

    def _fetch_global_product_image(
        self,
        barcode: str,
        product_name: str,
        brand: str,
        seed_pages: list[str] | None = None,
        serpapi_budget: SerpApiBudget | None = None,
    ) -> str | None:
        """
        Global product-image discovery.

        SerpAPI Google Images is used first because Gemini grounding often returns
        text but zero usable image URLs. If SerpAPI has no usable candidate, fall
        back to the older Gemini grounding/page-extraction path.
        """
        serpapi_image = self._fetch_reliable_product_image_url(barcode, product_name, brand, serpapi_budget)
        if serpapi_image:
            return serpapi_image

        log.info("Phase 1B – fallback global product image discovery via Gemini grounding …")
        facts, grounded_urls, _ = self._grounded_search(
            _GLOBAL_IMAGE_SEARCH_PROMPT.format(
                barcode=barcode,
                barcode_variants=_quoted_variants(barcode),
                product_name=product_name,
                brand=brand or "",
            )
        )

        candidates = self._extract_image_candidates_json(barcode, product_name, brand, facts)
        direct_urls: list[str] = []
        page_urls: list[str] = []

        def add_direct(u: str | None) -> None:
            u = _clean_public_source_url(u)
            if u and u not in direct_urls:
                direct_urls.append(u)

        def add_page(u: str | None) -> None:
            u = _clean_public_source_url(u)
            if u and u not in page_urls:
                page_urls.append(u)

        for item in candidates:
            if not isinstance(item, dict):
                continue
            conf = str(item.get("confidence") or "").lower()
            if conf and conf not in {"high", "medium"}:
                continue
            add_direct(item.get("image_url"))
            add_page(item.get("source_url"))

        for u in _clean_url_list(grounded_urls, limit=25) + _extract_candidate_urls_from_text(facts):
            path = urlparse(u).path.lower()
            if re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:$|[?])", path):
                add_direct(u)
            else:
                add_page(u)

        if not direct_urls and not page_urls:
            recovered_items, recovered_facts, recovered_urls = self._recover_global_image_candidates(
                barcode=barcode,
                product_name=product_name,
                brand=brand,
            )
            for item in recovered_items:
                if not isinstance(item, dict):
                    continue
                add_direct(item.get("image_url"))
                add_page(item.get("source_url"))
            for u in recovered_urls:
                path = urlparse(u).path.lower()
                if re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:$|[?])", path):
                    add_direct(u)
                else:
                    add_page(u)

        for u in seed_pages or []:
            add_page(u)

        for img_url in direct_urls[:20]:
            candidate = {"image_url": img_url, "source_url": "", "title": product_name}
            ok, final_url, image_bytes, mime_type, _score = _is_candidate_image_valid(candidate, product_name, brand, barcode)
            if ok and final_url and image_bytes and mime_type:
                if self._vision_verify_product_image(image_bytes, mime_type, product_name, brand, barcode):
                    log.info("Global product image selected from direct URL: %s", final_url)
                    return final_url

        for page_url in page_urls[:20]:
            img = _extract_best_image_from_page(page_url, product_name, brand)
            if img:
                log.info("Global product image selected from page: %s", page_url)
                return img

        log.warning("No valid global product image found.")
        return None

    def _resolve_global_product_image(
        self,
        barcode: str,
        prod_data: dict,
        retailers: list[RetailerListing],
        product_name: str,
        brand: str,
        seed_texts: list[str] | None = None,
        serpapi_budget: SerpApiBudget | None = None,
    ) -> str | None:
        """
        Resolve product image using GLOBAL product-image rules.

        Correct generic order:
          1. SerpAPI Google Images first, using verified product name/brand/barcode.
          2. If SerpAPI fails, try validated direct image_url from product identity.
          3. Try manufacturer/evidence/product pages already discovered.
          4. Final fallback to Gemini grounding/page image extraction.

        Pricing/location rules do not apply to image search.
        """
        # SerpAPI-first. This prevents a weaker Gemini/evidence image from
        # winning before Google Images candidates are checked.
        serpapi_image = self._fetch_reliable_product_image_url(
            barcode=barcode,
            product_name=product_name,
            brand=brand,
            serpapi_budget=serpapi_budget,
        )
        if serpapi_image:
            return serpapi_image

        seed_pages: list[str] = []
        for u in [prod_data.get("manufacturer_url"), *(prod_data.get("evidence_urls") or [])]:
            cleaned = _clean_public_source_url(u)
            if cleaned and cleaned not in seed_pages:
                seed_pages.append(cleaned)
        for text_blob in seed_texts or []:
            for u in _extract_candidate_urls_from_text(text_blob):
                cleaned = _clean_public_source_url(u)
                if cleaned and cleaned not in seed_pages:
                    seed_pages.append(cleaned)
        for r in retailers or []:
            cleaned = _clean_public_source_url(getattr(r, "url", None))
            if cleaned and cleaned not in seed_pages:
                seed_pages.append(cleaned)

        direct = _valid_direct_image_url(prod_data.get("image_url"), product_name, brand)
        if direct:
            return direct

        for page_url in seed_pages[:15]:
            image = _extract_best_image_from_page(page_url, product_name, brand)
            if image:
                log.info("Global product image selected from evidence page: %s", page_url)
                return image

        return self._fetch_global_product_image(barcode, product_name, brand, seed_pages, serpapi_budget)

    def _fetch_hk_retailers(
        self,
        barcode: str,
        product_name: str,
        brand: str,
        serpapi_budget: SerpApiBudget | None = None,
    ) -> list[RetailerListing]:
        valid: list[RetailerListing] = []
        existing: set[str] = set()

        # SerpAPI normal Google Search is the primary URL discovery layer for
        # HK buy pages. Gemini can still reason/extract, but it is not reliable
        # enough as the only URL source.
        serpapi_listings, serpapi_facts = _serpapi_hk_product_page_candidates(
            product_name=product_name,
            brand=brand or "",
            barcode=barcode,
            max_results=_env_int("SERPAPI_HK_MAX_CANDIDATES", 15),
            budget=serpapi_budget,
        )
        if serpapi_listings:
            log.info("SerpAPI HK URL discovery returned %d candidate(s).", len(serpapi_listings))
            for r in self._validate_retailers(serpapi_listings, barcode, product_name, brand, serpapi_facts):
                if r.url not in existing:
                    valid.append(r)
                    existing.add(r.url)
            if len(valid) >= _MIN_RETAILERS:
                return valid[:_MAX_RETAILERS]

        for attempt in range(1 + _EXTRA_SEARCH_ROUNDS):
            if attempt > 0:
                log.info("Retailer search round %d (valid so far: %d) …", attempt + 1, len(valid))
                time.sleep(1)

            log.info("Phase 2 – HK retailer search (attempt %d) …", attempt + 1)
            facts, all_urls, _ = self._grounded_search(
                _RETAILER_SEARCH_PROMPT.format(
                    barcode=barcode,
                    barcode_variants=_quoted_variants(barcode),
                    product_name=product_name,
                    brand=brand or "",
                )
            )

            candidate_urls = list(dict.fromkeys(
                _clean_url_list(all_urls, limit=25) + _extract_candidate_urls_from_text(facts)
            ))
            hk_urls = [
                u for u in candidate_urls
                if _looks_like_product_url(u) and not _host_matches(u, _BLOCKED_SOURCE_DOMAINS)
            ]

            log.info(
                "Grounding: %d total URLs → %d candidate product URLs%s",
                len(all_urls),
                len(hk_urls),
                ": " + str(hk_urls) if hk_urls else " (falling back to strict text extraction)",
            )

            if hk_urls:
                raw_listings = self._match_prices_to_urls(barcode, product_name, facts, hk_urls)
            else:
                raw_listings = self._extract_retailers_from_text(barcode, product_name, facts)

            # If Gemini produced notes but no usable direct product URLs, run one
            # URL-focused recovery pass. This is still not domain-list based:
            # recovered URLs must pass HK-signal + product-match + HKD-price validation.
            if not raw_listings:
                recovered_listings, recovered_facts, recovered_urls = self._recover_hk_retailer_candidates(
                    barcode=barcode,
                    product_name=product_name,
                    brand=brand,
                )
                if recovered_listings:
                    raw_listings = recovered_listings
                    facts = facts + "\n\n--- HK URL RECOVERY ---\n\n" + recovered_facts
                elif recovered_urls:
                    matched = self._match_prices_to_urls(barcode, product_name, recovered_facts, recovered_urls)
                    if matched:
                        raw_listings = matched
                        facts = facts + "\n\n--- HK URL RECOVERY ---\n\n" + recovered_facts

            for r in self._validate_retailers(raw_listings, barcode, product_name, brand, facts):
                if r.url not in existing:
                    valid.append(r)
                    existing.add(r.url)

            if len(valid) >= _MIN_RETAILERS:
                break

        if not valid:
            log.warning("No valid HK retailer found after %d attempt(s).", 1 + _EXTRA_SEARCH_ROUNDS)

        return valid[:_MAX_RETAILERS]


    @staticmethod
    def _coerce_nutrition_value(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            v = value.strip()
            return v or None
        if isinstance(value, (int, float)):
            return str(value)
        return str(value)

    @classmethod
    def _normalize_nutrition_raw(cls, nutri_raw: dict) -> tuple[dict, dict]:
        other = {}
        if isinstance(nutri_raw, dict):
            nutri_raw = dict(nutri_raw)
            other = nutri_raw.pop("other", {}) or {}
        else:
            nutri_raw = {}
        for key in [
            "crude_protein_min", "crude_fat_min", "crude_fiber_max",
            "moisture_max", "ash_max", "calories",
        ]:
            nutri_raw[key] = cls._coerce_nutrition_value(nutri_raw.get(key))
        if isinstance(other, dict):
            other = {str(k): cls._coerce_nutrition_value(v) or "" for k, v in other.items()}
        else:
            other = {}
        return nutri_raw, other

    def search(self, barcode: str) -> ProductInfo:
        """
        Final lookup orchestration.

        Correct separation of concerns:
          1. Product name/identity       = GLOBAL search, exact barcode evidence required.
          2. Nutrition/guaranteed values = GLOBAL search, exact product match required.
          3. Product image               = GLOBAL search, exact product match required.
          4. Pricing + buy URL           = HONG KONG online stores only, HKD price required.

        Only verified product identities are returned as cacheable ProductInfo.
        """
        # One per-lookup SerpAPI budget prevents a single barcode from burning many calls.
        serpapi_budget = SerpApiBudget()

        # ── Phase 1: GLOBAL product identity ──────────────────────
        prod_data, raw_facts = self._fetch_product_info(barcode, serpapi_budget=serpapi_budget)

        if not self._is_verified(prod_data):
            return ProductInfo(
                barcode=barcode,
                product_name="Unknown Product",
                barcode_verified=False,
                identity_confidence="low",
                evidence_urls=prod_data.get("evidence_urls") or [],
                barcode_evidence=prod_data.get("barcode_evidence"),
                warnings=prod_data.get("warnings") or [
                    "No exact source evidence connected this barcode to a product."
                ],
                raw_llm_response=raw_facts[:12000],
            )

        product_name = prod_data.get("product_name") or "Unknown Product"
        brand = prod_data.get("brand") or ""
        target_animal = prod_data.get("target_animal") or ""
        mfr_url = prod_data.get("manufacturer_url")

        # ── Phase 2: GLOBAL nutrition ─────────────────────────────
        # The identity phase may already return nutrition. If not, run a
        # dedicated global nutrition search before creating NutritionalInfo.
        nutri_raw, other = self._normalize_nutrition_raw(
            prod_data.get("nutritional_info") or {}
        )

        if not _nutrition_has_values({**nutri_raw, "other": other}):
            fallback_nutrition = self._fetch_global_nutrition(
                barcode=barcode,
                product_name=product_name,
                brand=brand,
            )
            if fallback_nutrition:
                nutri_raw, other = self._normalize_nutrition_raw(fallback_nutrition)

        try:
            nutritional = NutritionalInfo(**nutri_raw, other=other)
        except Exception as exc:
            log.warning("Nutrition parse failed after normalization: %s", exc)
            nutritional = NutritionalInfo()

        # ── Phase 3: GLOBAL product image ─────────────────────────
        # Do this before HK pricing so product image discovery does not become
        # accidentally limited to HK retailers.
        image_url = self._resolve_global_product_image(
            barcode=barcode,
            prod_data=prod_data,
            retailers=[],
            product_name=product_name,
            brand=brand,
            seed_texts=[raw_facts],
            serpapi_budget=serpapi_budget,
        )

        # ── Phase 4: HK-only pricing + buy URLs ───────────────────
        # This is the only phase where Hong Kong restrictions apply.
        retailers = self._fetch_hk_retailers(
            barcode=barcode,
            product_name=product_name,
            brand=brand,
            serpapi_budget=serpapi_budget,
        )

        return ProductInfo(
            barcode=barcode,
            product_name=product_name,
            brand=brand or None,
            target_animal=target_animal or None,
            manufacturer_url=mfr_url,
            image_url=image_url,
            nutritional_info=nutritional,
            hk_retailers=retailers,
            barcode_verified=True,
            identity_confidence=prod_data.get("identity_confidence", "medium"),
            evidence_urls=prod_data.get("evidence_urls") or [],
            barcode_evidence=prod_data.get("barcode_evidence"),
            warnings=prod_data.get("warnings") or [],
            raw_llm_response=raw_facts[:12000],
        )
