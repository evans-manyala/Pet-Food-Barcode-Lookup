"""
pinecone_store.py – Pinecone vector database integration.

Embedding source
----------------
  OpenAI text-embedding-3-small (1 536 dims) accessed via OpenRouter.
  OpenRouter is OpenAI-SDK-compatible — only the base_url changes.

Storage strategy
----------------
  • Vector ID     = barcode string (exact lookup in O(1))
  • Embedding     = product_name + brand + nutritional summary
  • Metadata      = full ProductInfo serialised as JSON string
  • Namespace     = "pet-food"

Lookup order (called from main.py)
-------------------
  1. fetch_by_barcode()  – exact ID match (fast, no embedding needed)
  2. semantic_search()   – cosine similarity fallback (rare, last resort)
"""

from __future__ import annotations
import json
import logging
from typing import Optional

from openai import OpenAI as _OAI          # OpenRouter is OAI-compatible
from pinecone import Pinecone, ServerlessSpec

from .config import get_settings
from .models import ProductInfo

log = logging.getLogger(__name__)

_EMBED_MODEL  = "openai/text-embedding-3-small"   # routed via OpenRouter
_EMBED_DIM    = 1536
_NAMESPACE    = "pet-food"
_TOP_K        = 3


def _build_embed_text(product: ProductInfo) -> str:
    """Concatenate key fields into a single string for embedding."""
    parts = [
        product.product_name,
        product.brand or "",
        product.target_animal or "",
        product.barcode,
    ]
    if product.nutritional_info:
        ni = product.nutritional_info
        parts.append(
            f"protein {ni.crude_protein_min} fat {ni.crude_fat_min} "
            f"fiber {ni.crude_fiber_max} moisture {ni.moisture_max}"
        )
    return " | ".join(p for p in parts if p)


class PineconeStore:
    """Upsert and query ProductInfo records from a Pinecone serverless index."""

    def __init__(self) -> None:
        cfg = get_settings()
        self._available = False

        if not cfg.pinecone_api_key:
            log.warning("PINECONE_API_KEY not set – Pinecone disabled.")
            return
        if not cfg.openrouter_api_key:
            log.warning("OPENROUTER_API_KEY not set – embeddings disabled (Pinecone skipped).")
            return

        try:
            # ── Pinecone client ───────────────────────────────────────────
            self._pc       = Pinecone(api_key=cfg.pinecone_api_key)
            self._idx_name = cfg.pinecone_index_name

            existing = [i.name for i in self._pc.list_indexes()]
            if self._idx_name not in existing:
                log.info("Creating Pinecone index '%s' …", self._idx_name)
                self._pc.create_index(
                    name=self._idx_name,
                    dimension=_EMBED_DIM,
                    metric="cosine",
                    spec=ServerlessSpec(cloud="aws", region="us-east-1"),
                )

            self._idx = self._pc.Index(self._idx_name)

            # ── OpenRouter client (for embeddings only) ───────────────────
            self._oai = _OAI(
                api_key=cfg.openrouter_api_key,
                base_url=cfg.openrouter_base_url,
            )

            log.info(
                "Pinecone ready (index='%s') | Embeddings via OpenRouter → %s",
                self._idx_name, _EMBED_MODEL,
            )
            self._available = True

        except Exception as exc:
            log.warning("Pinecone/OpenRouter initialisation failed: %s", exc)

    # ── Helpers ───────────────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        return self._available

    def _embed(self, text: str) -> list[float]:
        """Generate a 1 536-dim embedding via OpenRouter → OpenAI."""
        resp = self._oai.embeddings.create(model=_EMBED_MODEL, input=text)
        return resp.data[0].embedding

    def _to_metadata(self, product: ProductInfo) -> dict:
        raw = json.dumps(product.model_dump())
        if len(raw) > 39_000:          # Pinecone metadata cap ≈ 40 KB
            raw = raw[:39_000]
        return {"barcode": product.barcode, "payload": raw}

    def _from_metadata(self, metadata: dict) -> Optional[ProductInfo]:
        try:
            return ProductInfo(**json.loads(metadata["payload"]))
        except Exception as exc:
            log.warning("Failed to deserialise Pinecone metadata: %s", exc)
            return None

    # ── Public API ────────────────────────────────────────────────────────

    def upsert(self, product: ProductInfo) -> bool:
        """Store or update a product vector. Returns True on success."""
        if not self._available:
            return False
        try:
            text   = _build_embed_text(product)
            vector = self._embed(text)
            meta   = self._to_metadata(product)
            self._idx.upsert(
                vectors=[{
                    "id":       product.barcode,
                    "values":   vector,
                    "metadata": meta,
                }],
                namespace=_NAMESPACE,
            )
            log.info("Pinecone UPSERT barcode=%s", product.barcode)
            return True
        except Exception as exc:
            log.warning("Pinecone UPSERT error: %s", exc)
            return False

    def fetch_by_barcode(self, barcode: str) -> Optional[ProductInfo]:
        """Exact ID fetch — fastest path after a Redis miss."""
        if not self._available:
            return None
        try:
            result  = self._idx.fetch(ids=[barcode], namespace=_NAMESPACE)
            vectors = result.get("vectors") or {}
            if barcode not in vectors:
                log.debug("Pinecone exact MISS for barcode=%s", barcode)
                return None
            product = self._from_metadata(vectors[barcode].get("metadata", {}))
            if product:
                log.info("Pinecone exact HIT for barcode=%s", barcode)
            return product
        except Exception as exc:
            log.warning("Pinecone FETCH error: %s", exc)
            return None

    def semantic_search(self, query: str, top_k: int = _TOP_K) -> list[ProductInfo]:
        """
        Cosine-similarity search.  Used as a last-resort fallback when the
        exact barcode is unknown (e.g. barcode damaged / only product name
        available).
        """
        if not self._available:
            return []
        try:
            vec = self._embed(query)
            res = self._idx.query(
                vector=vec,
                top_k=top_k,
                include_metadata=True,
                namespace=_NAMESPACE,
            )
            products = []
            for match in res.get("matches", []):
                p = self._from_metadata(match.get("metadata", {}))
                if p:
                    products.append(p)
            log.info("Pinecone semantic search → %d result(s)", len(products))
            return products
        except Exception as exc:
            log.warning("Pinecone QUERY error: %s", exc)
            return []
