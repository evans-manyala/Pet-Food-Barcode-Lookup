"""
pending_cache.py – Retry Redis/Pinecone writes after outages or misconfiguration.

When a lookup succeeds but Redis or Pinecone is unavailable (or a write fails),
the product is appended to a JSONL queue on disk. The queue is drained on app
startup and after lookups once the target service responds again.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Literal

from src.config import get_settings
from src.models import ProductInfo
from src.pinecone_store import PineconeStore
from src.redis_cache import RedisCache

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STORE_PATH = "data/pending_cache_writes.jsonl"
Target = Literal["redis", "pinecone"]

_lock = threading.Lock()


def _resolve_store_path(raw: str | None = None) -> Path:
    value = (raw or _DEFAULT_STORE_PATH).strip() or _DEFAULT_STORE_PATH
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path.resolve()


def _settings() -> tuple[bool, Path, float]:
    try:
        cfg = get_settings()
        enabled = cfg.pending_cache_enabled
        path = _resolve_store_path(cfg.pending_cache_store_path)
        retention = max(1, int(cfg.pending_cache_retention_days)) * 86400
        return enabled, path, retention
    except Exception:
        return True, _resolve_store_path(), 7 * 86400


def pending_count() -> int:
    enabled, path, retention = _settings()
    if not enabled or not path.exists():
        return 0
    cutoff = time.time() - retention
    count = 0
    with _lock:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if float(row.get("ts") or 0) >= cutoff:
                count += 1
    return count


def enqueue(product: ProductInfo, target: Target) -> None:
    enabled, path, retention = _settings()
    if not enabled:
        return
    row = {
        "ts": time.time(),
        "barcode": product.barcode,
        "target": target,
        "product": product.model_dump(),
    }
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    log.info("Queued %s write for barcode=%s (service unavailable or write failed)", target, product.barcode)


def persist_product_caches(
    product: ProductInfo,
    redis_cache: RedisCache,
    pinecone: PineconeStore,
) -> dict[str, bool]:
    """Write to Redis/Pinecone now, or queue for a later flush."""
    results = {"redis": False, "pinecone": False}

    if redis_cache.is_available:
        results["redis"] = redis_cache.set(product)
    if not results["redis"]:
        enqueue(product, "redis")

    if pinecone.is_available:
        results["pinecone"] = pinecone.upsert(product)
    if not results["pinecone"]:
        enqueue(product, "pinecone")

    return results


def _load_pending_rows(path: Path, retention_seconds: float) -> dict[tuple[str, str], dict]:
    if not path.exists():
        return {}
    cutoff = time.time() - retention_seconds
    latest: dict[tuple[str, str], dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = float(row.get("ts") or 0)
        if ts < cutoff:
            continue
        barcode = str(row.get("barcode") or "").strip()
        target = row.get("target")
        product_data = row.get("product")
        if not barcode or target not in {"redis", "pinecone"} or not isinstance(product_data, dict):
            continue
        key = (barcode, target)
        if key not in latest or ts >= float(latest[key].get("ts") or 0):
            latest[key] = row
    return latest


def flush_pending_cache_writes() -> dict[str, int]:
    """
    Apply queued writes using fresh Redis/Pinecone clients.
    Returns counts: flushed, remaining, redis_ok, pinecone_ok.
    """
    enabled, path, retention = _settings()
    summary = {"flushed": 0, "remaining": 0, "redis_ok": 0, "pinecone_ok": 0}
    if not enabled:
        return summary

    with _lock:
        pending = _load_pending_rows(path, retention)
        if not pending:
            return summary

        redis_cache = RedisCache()
        pinecone = PineconeStore()
        still_pending: list[dict] = []

        for row in pending.values():
            try:
                product = ProductInfo(**row["product"])
            except Exception as exc:
                log.warning("Skipping invalid pending cache row for %s: %s", row.get("barcode"), exc)
                continue

            target: Target = row["target"]
            ok = False
            if target == "redis" and redis_cache.is_available:
                ok = redis_cache.set(product)
                if ok:
                    summary["redis_ok"] += 1
            elif target == "pinecone" and pinecone.is_available:
                ok = pinecone.upsert(product)
                if ok:
                    summary["pinecone_ok"] += 1

            if ok:
                summary["flushed"] += 1
            else:
                still_pending.append(row)

        summary["remaining"] = len(still_pending)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for row in still_pending:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(path)

    if summary["flushed"]:
        log.info(
            "Pending cache flush: %d written (%d redis, %d pinecone), %d remaining",
            summary["flushed"],
            summary["redis_ok"],
            summary["pinecone_ok"],
            summary["remaining"],
        )
    return summary
