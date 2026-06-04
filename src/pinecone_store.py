"""
pinecone_store.py – Pinecone vector database integration.

Storage strategy
----------------
  • Vector ID = exact barcode string
  • Metadata  = full ProductInfo JSON payload
  • Namespace = configurable via PINECONE_NAMESPACE

Safety note
-----------
ProductInfo now includes barcode_verified/identity_confidence. main.py will not
trust or display old Pinecone records unless they are verified.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from openai import OpenAI as _OAI
from pinecone import Pinecone, ServerlessSpec

from .config import get_settings
from .models import ProductInfo

log = logging.getLogger(__name__)

_TOP_K = 3


def _build_embed_text(product: ProductInfo) -> str:
    parts = [
        product.product_name,
        product.brand or "",
        product.target_animal or "",
        product.barcode,
        "barcode_verified" if product.barcode_verified else "barcode_unverified",
        product.identity_confidence,
    ]
    if product.nutritional_info:
        ni = product.nutritional_info
        parts.append(
            f"protein {ni.crude_protein_min} fat {ni.crude_fat_min} "
            f"fiber {ni.crude_fiber_max} moisture {ni.moisture_max} ash {ni.ash_max}"
        )
    if product.barcode_evidence:
        parts.append(product.barcode_evidence)
    return " | ".join(p for p in parts if p)


class PineconeStore:
    """Upsert and query ProductInfo records from a Pinecone serverless index."""

    def __init__(self) -> None:
        cfg = get_settings()
        self._available = False
        self._namespace = cfg.pinecone_namespace
        self._embed_model = cfg.embedding_model
        self._embed_dim = cfg.pinecone_dimension

        if not cfg.pinecone_api_key:
            log.warning("PINECONE_API_KEY not set – Pinecone disabled.")
            return
        if not cfg.openrouter_api_key:
            log.warning("OPENROUTER_API_KEY not set – embeddings disabled (Pinecone skipped).")
            return

        try:
            self._pc = Pinecone(api_key=cfg.pinecone_api_key)
            self._idx_name = cfg.pinecone_index_name

            existing = [i.name for i in self._pc.list_indexes()]
            if self._idx_name not in existing:
                log.info("Creating Pinecone index '%s' …", self._idx_name)
                self._pc.create_index(
                    name=self._idx_name,
                    dimension=self._embed_dim,
                    metric="cosine",
                    spec=ServerlessSpec(cloud="aws", region="us-east-1"),
                )

            self._idx = self._pc.Index(self._idx_name)

            self._oai = _OAI(
                api_key=cfg.openrouter_api_key,
                base_url=cfg.openrouter_base_url,
            )

            log.info(
                "Pinecone ready (index='%s', namespace='%s') | embeddings=%s",
                self._idx_name,
                self._namespace,
                self._embed_model,
            )
            self._available = True

        except Exception as exc:
            log.warning("Pinecone/OpenRouter initialisation failed: %s", exc)

    @property
    def is_available(self) -> bool:
        return self._available

    def _embed(self, text: str) -> list[float]:
        resp = self._oai.embeddings.create(model=self._embed_model, input=text)
        vector = resp.data[0].embedding
        if len(vector) != self._embed_dim:
            raise ValueError(
                f"Embedding dimension mismatch: got {len(vector)}, expected {self._embed_dim}"
            )
        return vector

    def _to_metadata(self, product: ProductInfo) -> dict:
        raw = product.model_dump_json()
        if len(raw) > 39_000:
            raw = raw[:39_000]
        return {
            "barcode": product.barcode,
            "barcode_verified": product.barcode_verified,
            "identity_confidence": product.identity_confidence,
            "product_name": product.product_name,
            "brand": product.brand or "",
            "payload": raw,
        }

    def _from_metadata(self, metadata: dict) -> Optional[ProductInfo]:
        try:
            return ProductInfo(**json.loads(metadata["payload"]))
        except Exception as exc:
            log.warning("Failed to deserialise Pinecone metadata: %s", exc)
            return None

    def upsert(self, product: ProductInfo) -> bool:
        """Store/update only a ProductInfo object. main.py decides whether it is safe."""
        if not self._available:
            return False
        try:
            text = _build_embed_text(product)
            vector = self._embed(text)
            meta = self._to_metadata(product)
            self._idx.upsert(
                vectors=[{
                    "id": product.barcode,
                    "values": vector,
                    "metadata": meta,
                }],
                namespace=self._namespace,
            )
            log.info("Pinecone UPSERT barcode=%s namespace=%s", product.barcode, self._namespace)
            return True
        except Exception as exc:
            log.warning("Pinecone UPSERT error: %s", exc)
            return False

    def fetch_by_barcode(self, barcode: str) -> Optional[ProductInfo]:
        """Exact ID fetch — fastest path after a Redis miss."""
        if not self._available:
            return None
        try:
            result = self._idx.fetch(ids=[barcode], namespace=self._namespace)
            vectors = result.get("vectors") or {}
            if barcode not in vectors:
                log.debug("Pinecone exact MISS for barcode=%s namespace=%s", barcode, self._namespace)
                return None
            product = self._from_metadata(vectors[barcode].get("metadata", {}))
            if product:
                log.info("Pinecone exact HIT for barcode=%s namespace=%s", barcode, self._namespace)
            return product
        except Exception as exc:
            log.warning("Pinecone FETCH error: %s", exc)
            return None

    def delete(self, barcode: str) -> bool:
        """Manually remove a product from Pinecone by barcode."""
        if not self._available:
            return False
        try:
            self._idx.delete(ids=[barcode], namespace=self._namespace)
            log.info("Pinecone DELETE barcode=%s namespace=%s", barcode, self._namespace)
            return True
        except Exception as exc:
            log.warning("Pinecone DELETE error: %s", exc)
            return False

    def semantic_search(self, query: str, top_k: int = _TOP_K) -> list[ProductInfo]:
        """
        Cosine similarity search. Use only as a manual/debug fallback because exact
        barcode lookup is safer for product identity.
        """
        if not self._available:
            return []
        try:
            vec = self._embed(query)
            res = self._idx.query(
                vector=vec,
                top_k=top_k,
                include_metadata=True,
                namespace=self._namespace,
            )
            products: list[ProductInfo] = []
            for match in res.get("matches", []):
                p = self._from_metadata(match.get("metadata", {}))
                if p:
                    products.append(p)
            log.info("Pinecone semantic search → %d result(s)", len(products))
            return products
        except Exception as exc:
            log.warning("Pinecone QUERY error: %s", exc)
            return []
