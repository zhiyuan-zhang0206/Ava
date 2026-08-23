"""Whole-response cache for the sidebar stats-dashboard route."""

from __future__ import annotations

import threading
import time

from gateway.schemas import StatsDashboard, StatsWindowHours

# The sidebar polls every 30 seconds. Caching the complete response for 60
# seconds avoids re-running its roughly 36-query Loki fan-out on every poll.
_CACHE_TTL_S = 60.0
_cache: dict[int, tuple[float, StatsDashboard]] = {}
_cache_lock = threading.Lock()


def cache_clear() -> None:
    """Test seam: drop all windowed dashboard responses."""
    with _cache_lock:
        _cache.clear()


def cache_get(hours: StatsWindowHours) -> StatsDashboard | None:
    """Return a fresh cached response for the requested window, if present."""
    with _cache_lock:
        hit = _cache.get(int(hours))
        if hit is None or hit[0] + _CACHE_TTL_S <= time.monotonic():
            return None
        return hit[1]


def cache_put(hours: StatsWindowHours, response: StatsDashboard) -> None:
    """Store the immutable response after its complete backend read succeeds."""
    with _cache_lock:
        _cache[int(hours)] = (time.monotonic(), response)
