"""
observability.py – In-memory lookup metrics for the monitoring dashboard.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

_MAX_EVENTS = 5_000

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

    @property
    def ts_iso(self) -> str:
        return datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat()

    @property
    def barcode_display(self) -> str:
        return self.barcode or ""

    @property
    def provider(self) -> str:
        return _PROVIDER_LABELS.get(self.source, self.source or "—")


class MetricsStore:
    def __init__(self) -> None:
        self._events: deque[LookupEvent] = deque(maxlen=_MAX_EVENTS)
        self._lock = threading.Lock()
        self._started_at = time.time()

    def record(self, event: LookupEvent) -> None:
        with self._lock:
            self._events.append(event)

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
        }

        return {
            "summary": summary,
            "hourly_volume": self._hourly_volume(events, hours),
            "failure_breakdown": self._failure_breakdown(failures),
            "provider_stats": self._provider_stats(events),
            "top_failing": self._top_failing(failures),
            "recent": [self._event_dict(e) for e in sorted(events, key=lambda x: x.ts, reverse=True)[:50]],
            "system": self._system_status(),
        }

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
            "cache_hit": e.cache_hit,
            "has_image": e.has_image,
            "unverified": e.unverified,
            "force_refresh": e.force_refresh,
        }

    def _system_status(self) -> dict:
        from src.pinecone_store import PineconeStore
        from src.redis_cache import RedisCache

        redis = RedisCache()
        pinecone = PineconeStore()
        return {
            "redis": "up" if redis.is_available else "down",
            "pinecone": "up" if pinecone.is_available else "down",
            "events_buffered": len(self._events),
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
