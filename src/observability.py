"""
observability.py – Lookup metrics for the monitoring dashboard.

Events are kept in memory for fast reads and appended to a JSONL file so the
dashboard survives API restarts.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_MAX_EVENTS = 5_000
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_METRICS_PATH = "data/lookup_metrics.jsonl"

_PROVIDER_LABELS = {
    "redis": "Redis",
    "pinecone": "Pinecone",
    "live_search": "Vertex AI",
    "": "—",
}


@dataclass
class LookupEvent:
    ts: float
    barcode: str
    success: bool
    error_type: str = ""
    source: str = ""
    response_ms: int = 0
    product_name: str = ""
    has_image: bool = False
    has_retailers: bool = False
    cache_hit: bool = False
    force_refresh: bool = False
    unverified: bool = False
    warning_count: int = 0
    identity_confidence: str = ""
    gemini_ms: int = 0
    serpapi_ms: int = 0
    catalog_ms: int = 0
    url_validation_ms: int = 0
    catalog_barcode_hits: int = 0
    catalog_retailer_candidates: int = 0
    catalog_trusted_retailers: int = 0

    @property
    def ts_iso(self) -> str:
        return datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat()

    @property
    def barcode_display(self) -> str:
        return self.barcode or ""

    @property
    def provider(self) -> str:
        return _PROVIDER_LABELS.get(self.source, self.source or "—")


def _event_from_dict(data: dict) -> LookupEvent | None:
    try:
        allowed = {f.name for f in fields(LookupEvent)}
        payload = {k: data[k] for k in allowed if k in data}
        if "ts" not in payload or "barcode" not in payload:
            return None
        payload["success"] = bool(payload.get("success", False))
        return LookupEvent(**payload)
    except Exception:
        return None


class MetricsStore:
    def __init__(self) -> None:
        self._events: deque[LookupEvent] = deque(maxlen=_MAX_EVENTS)
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._persistence_enabled = True
        self._retention_days = 30
        self._store_path = self._resolve_store_path()
        self._configure_storage()
        self._load_persisted_events()

    def _configure_storage(self) -> None:
        try:
            from src.config import get_settings

            settings = get_settings()
            self._persistence_enabled = settings.metrics_enabled
            self._retention_days = max(1, int(settings.metrics_retention_days))
            self._store_path = self._resolve_store_path(settings.metrics_store_path)
        except Exception as exc:
            log.warning("Metrics persistence config unavailable; using defaults: %s", exc)

    def _resolve_store_path(self, raw: str | None = None) -> Path:
        value = (raw or _DEFAULT_METRICS_PATH).strip() or _DEFAULT_METRICS_PATH
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = _PROJECT_ROOT / path
        return path.resolve()

    def _retention_cutoff(self) -> float:
        return time.time() - self._retention_days * 86400

    def _load_persisted_events(self) -> None:
        if not self._persistence_enabled:
            return

        path = self._store_path
        if not path.exists():
            return

        cutoff = self._retention_cutoff()
        loaded: list[LookupEvent] = []
        stale_lines = 0

        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = _event_from_dict(json.loads(line))
                    except json.JSONDecodeError:
                        stale_lines += 1
                        continue
                    if not event or event.ts < cutoff:
                        stale_lines += 1
                        continue
                    loaded.append(event)
        except Exception as exc:
            log.warning("Failed to load metrics store %s: %s", path, exc)
            return

        loaded.sort(key=lambda e: e.ts)
        if len(loaded) > _MAX_EVENTS:
            loaded = loaded[-_MAX_EVENTS:]

        with self._lock:
            self._events.extend(loaded)

        log.info(
            "Loaded %d persisted lookup metrics from %s",
            len(loaded),
            path.name,
        )

        if stale_lines or len(loaded) < self._count_file_lines(path):
            self._rewrite_store(loaded)

    def _count_file_lines(self, path: Path) -> int:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except Exception:
            return 0

    def _rewrite_store(self, events: list[LookupEvent]) -> None:
        if not self._persistence_enabled:
            return

        path = self._store_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
            tmp.replace(path)
        except Exception as exc:
            log.warning("Failed to compact metrics store %s: %s", path, exc)

    def _append_persisted(self, event: LookupEvent) -> None:
        if not self._persistence_enabled:
            return

        path = self._store_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning("Failed to persist lookup metric: %s", exc)

    def record(self, event: LookupEvent) -> None:
        with self._lock:
            self._events.append(event)
        self._append_persisted(event)

    def _events_in_window(self, hours: float) -> list[LookupEvent]:
        cutoff = time.time() - hours * 3600
        with self._lock:
            return [e for e in self._events if e.ts >= cutoff]

    def get_stats(self, hours: float = 24.0) -> dict[str, Any]:
        events = self._events_in_window(hours)
        total = len(events)
        successes = [e for e in events if e.success]
        failures = [e for e in events if not e.success]

        cache_hits = [e for e in events if e.cache_hit]
        with_image = sum(1 for e in successes if e.has_image)
        with_retailers = sum(1 for e in successes if e.has_retailers)
        unverified = sum(1 for e in events if e.unverified)
        force_refresh = sum(1 for e in events if e.force_refresh)
        warnings = sum(e.warning_count for e in events)

        response_times = [e.response_ms for e in events if e.response_ms > 0]
        ok_times = [e.response_ms for e in successes if e.response_ms > 0]

        live_events = [e for e in events if e.source == "live_search" and not e.cache_hit]

        summary = {
            "window_hours": int(hours),
            "total": total,
            "successes": len(successes),
            "failures": len(failures),
            "success_rate": round(len(successes) / total * 100, 1) if total else 0.0,
            "cache_hit_rate": round(len(cache_hits) / total * 100, 1) if total else 0.0,
            "avg_response_ms": round(sum(response_times) / len(response_times)) if response_times else 0,
            "avg_response_ms_ok": round(sum(ok_times) / len(ok_times)) if ok_times else 0,
            "with_image": with_image,
            "with_retailers": with_retailers,
            "unverified": unverified,
            "force_refresh": force_refresh,
            "warnings": warnings,
            "uptime_seconds": int(time.time() - self._started_at),
            "catalog_barcode_hits": sum(e.catalog_barcode_hits for e in live_events),
            "catalog_retailer_candidates": sum(e.catalog_retailer_candidates for e in live_events),
            "catalog_trusted_retailers": sum(e.catalog_trusted_retailers for e in live_events),
        }

        return {
            "summary": summary,
            "hourly_volume": self._hourly_volume(events, hours),
            "failure_breakdown": self._failure_breakdown(failures),
            "provider_stats": self._provider_stats(events),
            "pipeline_timing": self._pipeline_timing(live_events),
            "top_failing": self._top_failing(failures),
            "recent": [self._event_dict(e) for e in sorted(events, key=lambda x: x.ts, reverse=True)[:50]],
            "system": self._system_status(),
        }

    def _pipeline_timing(self, events: list[LookupEvent]) -> list[dict]:
        if not events:
            return []

        buckets = [
            ("gemini_ms", "Gemini / Vertex AI"),
            ("serpapi_ms", "SerpAPI"),
            ("catalog_ms", "Local catalog"),
            ("url_validation_ms", "URL validation"),
        ]
        rows = []
        for key, label in buckets:
            values = [getattr(e, key, 0) for e in events if getattr(e, key, 0) > 0]
            if not values:
                continue
            rows.append({
                "stage": label,
                "calls": len(values),
                "avg_ms": round(sum(values) / len(values)),
                "max_ms": max(values),
                "total_ms": sum(values),
            })
        return rows

    def _hourly_volume(self, events: list[LookupEvent], hours: float) -> list[dict]:
        bucket_count = max(1, min(int(hours), 168))
        bucket_secs = hours * 3600 / bucket_count
        now = time.time()
        start = now - hours * 3600
        buckets: list[dict] = []

        for i in range(bucket_count):
            b_start = start + i * bucket_secs
            b_end = b_start + bucket_secs
            in_bucket = [e for e in events if b_start <= e.ts < b_end]
            label = datetime.fromtimestamp(b_start, tz=timezone.utc).strftime("%H:%M")
            if hours > 48:
                label = datetime.fromtimestamp(b_start, tz=timezone.utc).strftime("%m/%d %H:%M")
            buckets.append({
                "label": label,
                "successes": sum(1 for e in in_bucket if e.success),
                "failures": sum(1 for e in in_bucket if not e.success),
            })
        return buckets

    def _failure_breakdown(self, failures: list[LookupEvent]) -> list[dict]:
        counts: dict[str, list[str]] = defaultdict(list)
        for e in failures:
            key = e.error_type or "unknown"
            counts[key].append(e.product_name or e.error_type)

        rows = []
        for error_type, samples in sorted(counts.items(), key=lambda x: -len(x[1])):
            rows.append({
                "error_type": error_type,
                "count": len(samples),
                "samples": ", ".join(s[:60] for s in samples[:3]),
            })
        return rows

    def _provider_stats(self, events: list[LookupEvent]) -> list[dict]:
        by_source: dict[str, list[LookupEvent]] = defaultdict(list)
        for e in events:
            if e.source:
                by_source[e.source].append(e)

        rows = []
        for source, evs in sorted(by_source.items(), key=lambda x: -len(x[1])):
            times = [e.response_ms for e in evs if e.response_ms > 0]
            ok = sum(1 for e in evs if e.success)
            rows.append({
                "provider": _PROVIDER_LABELS.get(source, source),
                "calls": len(evs),
                "successes": ok,
                "avg_ms": round(sum(times) / len(times)) if times else 0,
                "max_ms": max(times) if times else 0,
            })
        return rows

    def _top_failing(self, failures: list[LookupEvent]) -> list[dict]:
        by_barcode: dict[str, dict] = {}
        for e in failures:
            if e.barcode not in by_barcode:
                by_barcode[e.barcode] = {"attempts": 0, "errors": set()}
            by_barcode[e.barcode]["attempts"] += 1
            by_barcode[e.barcode]["errors"].add(e.error_type or "unknown")

        rows = []
        for barcode, data in sorted(by_barcode.items(), key=lambda x: -x[1]["attempts"])[:10]:
            rows.append({
                "barcode": barcode,
                "attempts": data["attempts"],
                "error_types": ", ".join(sorted(data["errors"])),
            })
        return rows

    def _event_dict(self, e: LookupEvent) -> dict:
        return {
            "ts_iso": e.ts_iso,
            "barcode_display": e.barcode_display,
            "success": e.success,
            "error_type": e.error_type,
            "product_name": e.product_name,
            "provider": e.provider,
            "response_ms": e.response_ms,
            "gemini_ms": e.gemini_ms,
            "serpapi_ms": e.serpapi_ms,
            "catalog_ms": e.catalog_ms,
            "url_validation_ms": e.url_validation_ms,
            "cache_hit": e.cache_hit,
            "has_image": e.has_image,
            "unverified": e.unverified,
            "force_refresh": e.force_refresh,
            "catalog_trusted_retailers": e.catalog_trusted_retailers,
        }

    def _system_status(self) -> dict:
        from src.catalog import get_catalog_store
        from src.pinecone_store import PineconeStore
        from src.redis_cache import RedisCache

        redis = RedisCache()
        pinecone = PineconeStore()
        catalog = get_catalog_store().get_stats()
        with self._lock:
            buffered = len(self._events)
        return {
            "redis": "up" if redis.is_available else "down",
            "pinecone": "up" if pinecone.is_available else "down",
            "catalog": "up" if catalog.get("available") else "down",
            "catalog_listings": catalog.get("listings", 0),
            "catalog_barcode_indexed": catalog.get("barcode_indexed", 0),
            "catalog_last_import_at": catalog.get("last_import_at"),
            "events_buffered": buffered,
            "metrics_persisted": self._persistence_enabled,
            "metrics_store_path": str(self._store_path) if self._persistence_enabled else "",
        }


metrics = MetricsStore()


def classify_error(status_code: int, error_msg: str = "", success: bool = True) -> str:
    if success:
        return ""
    msg = (error_msg or "").lower()
    if status_code == 400:
        return "invalid_barcode"
    if status_code == 503:
        return "service_unavailable"
    if status_code == 500:
        return "server_error"
    if "not safely identified" in msg or "unverified" in msg:
        return "unverified"
    if status_code == 404 or "not found" in msg:
        return "not_found"
    if "gemini" in msg or "vertex" in msg or "llm" in msg:
        return "llm_error"
    return "not_found"
