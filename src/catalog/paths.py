"""Resolve HK catalog CSV import directories (portable across machines)."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Default layout inside the repo — copy scraped CSV folders here on any PC/VM.
DEFAULT_HKTVMALL_IMPORT_DIR = "data/imports/hktvmall"
DEFAULT_SHOPIFY_IMPORT_DIR = "data/imports/shopify"


def resolve_import_dir(raw: str | None, default_relative: str) -> Path:
    """
    Resolve an import directory from .env or CLI.

    - Relative paths are anchored to the project root (portable in git clones).
    - Absolute paths and ``~`` are expanded as-is.
    - Empty/missing values fall back to ``default_relative``.
    """
    value = (raw or "").strip() or default_relative
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def default_hktvmall_import_dir() -> Path:
    return resolve_import_dir(None, DEFAULT_HKTVMALL_IMPORT_DIR)


def default_shopify_import_dir() -> Path:
    return resolve_import_dir(None, DEFAULT_SHOPIFY_IMPORT_DIR)
