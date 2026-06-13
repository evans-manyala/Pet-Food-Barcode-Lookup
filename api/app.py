"""
api/app.py – FastAPI web service for Pet Food Barcode Lookup.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.barcode_validator import validate_barcode
from src.config import get_settings
from src.llm_searcher import (
    _FACTS_PRIORITY_HEADER,
    _PRODUCT_EXTRACT_PROMPT,
    _PRODUCT_SEARCH_PROMPT,
    _barcode_variants,
    _quoted_variants,
)
from src.observability import LookupEvent, classify_error, metrics
from src.pending_cache import flush_pending_cache_writes
from src.serialization import product_to_api_payload
from src.service import lookup_barcode

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    flush_pending_cache_writes()
    yield
    flush_pending_cache_writes()


app = FastAPI(
    title="Pet Food Barcode Lookup",
    description=(
        "Look up pet food products by barcode with Hong Kong retailer pricing, "
        "nutrition, and verified identity.\n\n"
        "**Lookup pipeline:** Redis cache → Pinecone → Gemini live search.\n\n"
        "Full docs: [docs/API.md](https://github.com/evans-manyala/Pet-Food-Barcode-Lookup/blob/main/docs/API.md)"
    ),
    version="1.0.0",
    lifespan=_app_lifespan,
    openapi_tags=[
        {"name": "health", "description": "Liveness and readiness"},
        {"name": "lookup", "description": "Barcode product lookup"},
        {"name": "stats", "description": "Observability metrics for /dashboard"},
        {"name": "ui", "description": "Web frontend (HTML)"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")


class LookupRequest(BaseModel):
    barcode: str = Field(..., min_length=1, description="EAN-13, UPC-A, or EAN-8 barcode")
    force_refresh: bool = Field(False, description="Bypass Redis/Pinecone and fetch live")


def _check_stats_token(token: str | None) -> None:
    cfg = get_settings()
    if cfg.stats_token and token != cfg.stats_token:
        raise HTTPException(status_code=401, detail="Invalid or missing stats token")


def _record_lookup(
    *,
    barcode: str,
    force_refresh: bool,
    started: float,
    success: bool,
    source: str = "",
    error_type: str = "",
    product_name: str = "",
    has_image: bool = False,
    has_retailers: bool = False,
    unverified: bool = False,
    warning_count: int = 0,
    identity_confidence: str = "",
    timings: dict | None = None,
    catalog_stats: dict | None = None,
) -> None:
    timings = timings or {}
    catalog_stats = catalog_stats or {}
    metrics.record(LookupEvent(
        ts=time.time(),
        barcode=barcode,
        success=success,
        error_type=error_type,
        source=source,
        response_ms=int((time.time() - started) * 1000),
        product_name=product_name,
        has_image=has_image,
        has_retailers=has_retailers,
        cache_hit=source in {"redis", "pinecone"},
        force_refresh=force_refresh,
        unverified=unverified,
        warning_count=warning_count,
        identity_confidence=identity_confidence,
        gemini_ms=int(timings.get("gemini_ms") or 0),
        serpapi_ms=int(timings.get("serpapi_ms") or 0),
        catalog_ms=int(timings.get("catalog_ms") or 0),
        url_validation_ms=int(timings.get("url_validation_ms") or 0),
        catalog_barcode_hits=int(catalog_stats.get("barcode_hits") or 0),
        catalog_retailer_candidates=int(catalog_stats.get("retailer_candidates") or 0),
        catalog_trusted_retailers=int(catalog_stats.get("trusted_retailers") or 0),
        passes_used=int(timings.get("passes_used") or 0),
        grounding_queries=list(timings.get("grounding_queries") or []),
    ))


@app.get("/api/health", tags=["health"])
def health() -> dict:
    return {"status": "ok", "service": "pet-food-barcode-lookup"}


@app.get("/api/stats", tags=["stats"])
def stats(
    hours: float = Query(24, ge=1, le=168),
    token: str | None = Query(None),
) -> dict:
    _check_stats_token(token)
    return metrics.get_stats(hours=hours)


@app.get("/api/lookup", tags=["lookup"])
def lookup_get(
    barcode: str = Query(..., min_length=1),
    force_refresh: bool = Query(False),
) -> dict:
    return _do_lookup(barcode, force_refresh)


@app.post("/api/lookup", tags=["lookup"])
def lookup_post(body: LookupRequest) -> dict:
    return _do_lookup(body.barcode, body.force_refresh)


def _do_lookup(raw_barcode: str, force_refresh: bool) -> dict:
    started = time.time()
    barcode_for_metrics = raw_barcode.strip()

    result = validate_barcode(barcode_for_metrics)
    if not result.is_valid:
        _record_lookup(
            barcode=barcode_for_metrics,
            force_refresh=force_refresh,
            started=started,
            success=False,
            error_type="invalid_barcode",
        )
        raise HTTPException(status_code=400, detail=result.error)

    barcode_for_metrics = result.barcode

    lookup_timings: dict | None = None
    lookup_catalog: dict | None = None

    try:
        lookup = lookup_barcode(result.barcode, force_refresh=force_refresh)
        lookup_timings = lookup.timings
        lookup_catalog = lookup.catalog_stats
    except EnvironmentError as exc:
        _record_lookup(
            barcode=barcode_for_metrics,
            force_refresh=force_refresh,
            started=started,
            success=False,
            error_type="service_unavailable",
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        err = str(exc)
        if "DefaultCredentialsError" in type(exc).__name__ or "default credentials were not found" in err.lower():
            _record_lookup(
                barcode=barcode_for_metrics,
                force_refresh=force_refresh,
                started=started,
                success=False,
                error_type="service_unavailable",
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "Google Cloud credentials are not configured. "
                    "Run: gcloud auth application-default login"
                ),
            ) from exc
        _record_lookup(
            barcode=barcode_for_metrics,
            force_refresh=force_refresh,
            started=started,
            success=False,
            error_type="server_error",
        )
        raise

    if lookup.product is None or lookup.error:
        detail = lookup.error or "Product not found"
        product = lookup.product
        _record_lookup(
            barcode=barcode_for_metrics,
            force_refresh=force_refresh,
            started=started,
            success=False,
            source=lookup.source,
            error_type=classify_error(404 if product is None else 200, detail, success=False),
            product_name=product.product_name if product else "",
            has_image=bool(product and product.image_url),
            has_retailers=bool(product and product.hk_retailers),
            unverified=True,
            warning_count=len(product.warnings) if product else 0,
            identity_confidence=product.identity_confidence if product else "",
            timings=lookup_timings,
            catalog_stats=lookup_catalog,
        )
        if product:
            return {
                "success": False,
                "error": detail,
                "data": product_to_api_payload(product, source=lookup.source),
            }
        raise HTTPException(status_code=404, detail=detail)

    product = lookup.product
    _record_lookup(
        barcode=barcode_for_metrics,
        force_refresh=force_refresh,
        started=started,
        success=True,
        source=lookup.source,
        product_name=product.product_name,
        has_image=bool(product.image_url),
        has_retailers=bool(product.hk_retailers),
        warning_count=len(product.warnings),
        identity_confidence=product.identity_confidence,
        timings=lookup_timings,
        catalog_stats=lookup_catalog,
    )

    return {
        "success": True,
        "data": product_to_api_payload(product, source=lookup.source),
    }


@app.get("/api/debug/barcode/{barcode}", tags=["stats"])
def debug_barcode(barcode: str, token: str | None = None) -> dict:
    """
    Return the exact Vertex AI prompts and any recorded grounding queries for a barcode.
    Useful for diagnosing why a barcode search failed or took multiple passes.
    """
    _check_stats_token(token)

    variants = _barcode_variants(barcode)
    quoted = _quoted_variants(barcode)

    search_prompt = _PRODUCT_SEARCH_PROMPT.format(
        barcode=barcode,
        barcode_variants=quoted,
    )
    extract_prompt_template = _PRODUCT_EXTRACT_PROMPT.format(
        barcode=barcode,
        barcode_variants=quoted,
        facts="<research_notes_injected_here>",
    )

    # Pull the most recent lookup event for this barcode from the metrics store.
    recent_events = metrics.get_stats(hours=168)["recent"]
    last_event = next(
        (e for e in recent_events if e.get("barcode_display") == barcode),
        None,
    )

    return {
        "barcode": barcode,
        "variants": variants,
        "prompts": {
            "pass_1_search_prompt": search_prompt,
            "extract_prompt_template": extract_prompt_template,
            "multi_pass_facts_header": _FACTS_PRIORITY_HEADER,
        },
        "last_lookup": last_event,
    }


@app.get("/", tags=["ui"])
def index() -> FileResponse:
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return FileResponse(index_path)


@app.get("/dashboard", tags=["ui"])
def dashboard() -> FileResponse:
    dash_path = FRONTEND_DIR / "dashboard.html"
    if not dash_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return FileResponse(dash_path)


def run() -> None:
    import uvicorn

    cfg = get_settings()
    uvicorn.run(
        "api.app:app",
        host=cfg.api_host,
        port=cfg.api_port,
        reload=cfg.api_reload,
    )


if __name__ == "__main__":
    run()
