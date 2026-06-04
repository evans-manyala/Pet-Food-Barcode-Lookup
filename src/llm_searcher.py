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
import re
import time
import html as html_lib
from typing import Iterable
from urllib.parse import urlparse, quote, urljoin

import requests as http_requests
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
    ]
    return any(marker in plain for marker in hk_markers)


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

    def _fetch_product_info(self, barcode: str) -> tuple[dict, str]:
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
                # Fallback path: only use search-snippet validation for blocked/JS cases,
                # not for URLs that actually return 404/not-found content.
                probe_ok, _probe_final, probe_html, probe_status = _get_page_text(url)
                if probe_status == 404 or (probe_html and _page_is_not_found(probe_html)):
                    log.info("  Dead/error/wrong product URL dropped: %s", url)
                    continue

                snippet_context = _context_for_url(facts, url)
                snippet_ok, snippet_price, snippet_stock = _search_snippet_valid_listing(
                    url,
                    "\n".join([str(item), snippet_context]),
                    barcode,
                    product_name,
                    brand,
                )
                if not snippet_ok:
                    log.info("  Dead/error/wrong product URL dropped: %s", url)
                    continue
                if not item.get("price_hkd") and snippet_price:
                    item["price_hkd"] = snippet_price
                if item.get("in_stock") is None and snippet_stock is not None:
                    item["in_stock"] = snippet_stock
                item["notes"] = (item.get("notes") or "") + " | Price/URL validated from search snippet; live page check failed or was JS/protected."

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

    def _fetch_global_product_image(
        self,
        barcode: str,
        product_name: str,
        brand: str,
        seed_pages: list[str] | None = None,
    ) -> str | None:
        """
        Global product-image discovery.

        Unlike retailer/pricing lookup, this is intentionally NOT restricted to
        Hong Kong. It only validates that the image is a real, non-placeholder
        product image and that the source/candidate text matches the verified
        product identity.
        """
        log.info("Phase 1B – global product image discovery …")
        facts, grounded_urls, _ = self._grounded_search(
            _GLOBAL_IMAGE_SEARCH_PROMPT.format(
                barcode=barcode,
                barcode_variants=_quoted_variants(barcode),
                product_name=product_name,
                brand=brand or "",
            )
        )

        candidates = self._extract_image_candidates_json(barcode, product_name, brand, facts)

        # First try direct image URLs from structured candidates and raw notes.
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

        # Raw URL extraction catches cases where Gemini mentions direct URLs in notes
        # but did not put them into the structured JSON.
        for u in _clean_url_list(grounded_urls, limit=25) + _extract_candidate_urls_from_text(facts):
            path = urlparse(u).path.lower()
            if re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:$|[?])", path):
                add_direct(u)
            else:
                add_page(u)

        # If the first image search produced text but no usable URLs, run one
        # URL-focused recovery pass. This mirrors a normal Google product-image
        # search using the already verified product name, but every result still
        # goes through Python image validation.
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
            valid = _valid_direct_image_url(img_url, product_name, brand)
            if valid:
                log.info("Global product image selected from direct URL: %s", valid)
                return valid

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
    ) -> str | None:
        """
        Resolve image using global product-image rules, while keeping pricing HK-only.

        Order:
        1. Existing direct image_url if valid.
        2. Manufacturer/evidence/retailer pages already discovered.
        3. Fresh global image search via Gemini grounding.
        """
        local = _resolve_product_image(prod_data, retailers, product_name, brand)
        if local:
            return local

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

        return self._fetch_global_product_image(barcode, product_name, brand, seed_pages)

    def _fetch_hk_retailers(
        self,
        barcode: str,
        product_name: str,
        brand: str,
    ) -> list[RetailerListing]:
        valid: list[RetailerListing] = []
        existing: set[str] = set()

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
        # ── Phase 1: GLOBAL product identity ──────────────────────
        prod_data, raw_facts = self._fetch_product_info(barcode)

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
        )

        # ── Phase 4: HK-only pricing + buy URLs ───────────────────
        # This is the only phase where Hong Kong restrictions apply.
        retailers = self._fetch_hk_retailers(
            barcode=barcode,
            product_name=product_name,
            brand=brand,
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
