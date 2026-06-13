"""
config.py – Centralised settings loaded from .env.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Gemini via Vertex AI (uses GCP billing + ADC) ──────────────────────
    google_cloud_project: str = ""
    google_cloud_location: str = "us-central1"
    gemini_model: str = "gemini-2.5-flash"

    # ── OpenRouter (for OpenAI-compatible embeddings → Pinecone) ──────────
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    embedding_model: str = "openai/text-embedding-3-small"

    # ── Redis ──────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    redis_ttl: int = 86_400  # 24 h

    # ── Pinecone ───────────────────────────────────────────────────────────
    pinecone_api_key: str = ""
    pinecone_index_name: str = "pet-food-products"
    pinecone_dimension: int = 1536
    pinecone_namespace: str = "pet-food"

    # ── Local HK retailer catalog (scraped CSV import) ─────────────────────
    hk_catalog_enabled: bool = True
    hk_catalog_db_path: str = "data/hk_retailer_catalog.db"
    # Relative paths are resolved from the project root (works on any machine).
    # Copy CSV scrapes into data/imports/hktvmall/ and data/imports/shopify/
    # or override with absolute paths on a specific PC/VM.
    hk_catalog_import_hktvmall_dir: str = "data/imports/hktvmall"
    hk_catalog_import_shopify_dir: str = "data/imports/shopify"
    hk_catalog_import_master_scrape_dir: str = "data/imports/master_scrape"
    hk_catalog_overrides_path: str = "data/barcode_overrides.json"

    # ── Dashboard metrics (persisted across API restarts) ───────────────────
    metrics_enabled: bool = True
    metrics_store_path: str = "data/lookup_metrics.jsonl"
    metrics_retention_days: int = 30

    # ── Pending Redis/Pinecone writes (retry after outage or .env misconfig) ─
    pending_cache_enabled: bool = True
    pending_cache_store_path: str = "data/pending_cache_writes.jsonl"
    pending_cache_retention_days: int = 7

    # ── App ────────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_reload: bool = False
    stats_token: str = ""  # optional: protect /api/stats and /dashboard?token=


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
