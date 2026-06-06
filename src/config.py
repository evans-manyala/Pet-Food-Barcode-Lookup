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

    # ── App ────────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_reload: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
