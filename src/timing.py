"""Per-lookup pipeline timing (Gemini, SerpAPI, catalog, URL validation)."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

_current_timer: ContextVar[LookupTimer | None] = ContextVar("lookup_timer", default=None)


@dataclass
class LookupTimer:
    gemini_ms: int = 0
    serpapi_ms: int = 0
    catalog_ms: int = 0
    url_validation_ms: int = 0
    wall_ms: int = 0

    @contextmanager
    def measure(self, bucket: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = int((time.perf_counter() - start) * 1000)
            current = getattr(self, f"{bucket}_ms", 0)
            setattr(self, f"{bucket}_ms", current + elapsed)

    def finish(self, started: float) -> None:
        self.wall_ms = int((time.perf_counter() - started) * 1000)

    @property
    def other_ms(self) -> int:
        measured = (
            self.gemini_ms
            + self.serpapi_ms
            + self.catalog_ms
            + self.url_validation_ms
        )
        base = self.wall_ms or measured
        return max(0, base - measured)

    @property
    def total_ms(self) -> int:
        measured = (
            self.gemini_ms
            + self.serpapi_ms
            + self.catalog_ms
            + self.url_validation_ms
            + self.other_ms
        )
        return self.wall_ms or measured

    def to_dict(self) -> dict[str, int | float]:
        total = max(self.total_ms, 1)
        return {
            "gemini_ms": self.gemini_ms,
            "serpapi_ms": self.serpapi_ms,
            "catalog_ms": self.catalog_ms,
            "url_validation_ms": self.url_validation_ms,
            "other_ms": self.other_ms,
            "total_ms": self.total_ms,
            "gemini_pct": round(self.gemini_ms / total * 100, 1),
            "serpapi_pct": round(self.serpapi_ms / total * 100, 1),
            "catalog_pct": round(self.catalog_ms / total * 100, 1),
            "url_validation_pct": round(self.url_validation_ms / total * 100, 1),
        }


def get_lookup_timer() -> LookupTimer | None:
    return _current_timer.get()


def activate_lookup_timer(timer: LookupTimer):
    return _current_timer.set(timer)


def deactivate_lookup_timer(token) -> None:
    _current_timer.reset(token)
