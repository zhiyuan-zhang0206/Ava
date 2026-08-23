"""Cached Loki aggregates used only by the sidebar stats-dashboard route."""

from __future__ import annotations

import threading
import time
from datetime import datetime

from gateway import loki_events
from gateway.routers import _inspect_pg
from gateway.schemas import StatsWindowHours

# The sidebar polls every 5s. Keep the expensive, four-field llm_usage read
# bounded to one result per requested window every 30s; turn/error/warning
# aggregates deliberately remain fresh in status.py.
_CACHE_TTL_S = 30.0
_cache: dict[tuple[int], tuple[float, dict[str, float]]] = {}
_cache_lock = threading.Lock()


def cache_clear() -> None:
    """Test seam: drop the windowed llm_usage aggregate cache."""
    with _cache_lock:
        _cache.clear()


def llm_usage_sums(
    hours: StatsWindowHours, window_start: datetime, now: datetime
) -> dict[str, float]:
    """Return cached telemetry-only llm_usage sums for one sidebar window."""
    cache_key = (int(hours),)
    now_mono = time.monotonic()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit is not None and hit[0] + _CACHE_TTL_S > now_mono:
            return hit[1]

    sums = {
        field: sum(
            _inspect_pg.query_loki_shards(
                window_start,
                now,
                lambda shard_start, shard_end, field=field: loki_events.attribute_aggregate(
                    field=field,
                    agg="sum",
                    event_names=["llm_usage"],
                    categories=["telemetry"],
                    from_=shard_start,
                    to=shard_end,
                ),
            )
        )
        for field in ("in_total", "out_total", "cache_read", "cost_usd")
    }
    with _cache_lock:
        _cache[cache_key] = (time.monotonic(), sums)
    return sums
