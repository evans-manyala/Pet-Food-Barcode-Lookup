"""
api/app.py – FastAPI web service for Pet Food Barcode Lookup.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.barcode_validator import validate_barcode
from src.config import get_settings
from src.serialization import product_to_api_payload
from src.service import lookup_barcode

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(
    title="Pet Food Barcode Lookup",
    description="Look up pet food products by barcode with HK retailer pricing.",
    version="1.0.0",
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


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "pet-food-barcode-lookup"}


@app.get("/api/lookup")
def lookup_get(
    barcode: str = Query(..., min_length=1),
    force_refresh: bool = Query(False),
) -> dict:
    return _do_lookup(barcode, force_refresh)


@app.post("/api/lookup")
def lookup_post(body: LookupRequest) -> dict:
    return _do_lookup(body.barcode, body.force_refresh)


def _do_lookup(raw_barcode: str, force_refresh: bool) -> dict:
    result = validate_barcode(raw_barcode.strip())
    if not result.is_valid:
        raise HTTPException(status_code=400, detail=result.error)

    try:
        lookup = lookup_barcode(result.barcode, force_refresh=force_refresh)
    except EnvironmentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        err = str(exc)
        if "DefaultCredentialsError" in type(exc).__name__ or "default credentials were not found" in err.lower():
            raise HTTPException(
                status_code=503,
                detail=(
                    "Google Cloud credentials are not configured. "
                    "Run: gcloud auth application-default login"
                ),
            ) from exc
        raise

    if lookup.product is None or lookup.error:
        detail = lookup.error or "Product not found"
        if lookup.product:
            return {
                "success": False,
                "error": detail,
                "data": product_to_api_payload(lookup.product, source=lookup.source),
            }
        raise HTTPException(status_code=404, detail=detail)

    return {
        "success": True,
        "data": product_to_api_payload(lookup.product, source=lookup.source),
    }


@app.get("/")
def index() -> FileResponse:
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return FileResponse(index_path)


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
