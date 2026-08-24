"""Short result cache for repeated Loki dashboard aggregations.

Dashboard clients poll every 30 seconds and re-issue identical aggregation
shapes. Flooring both window bounds to minute identifiers makes those poll
keys stable, while also deduplicating shared clock-aligned shards across
different windows. A result can therefore be up to 60 seconds stale, which is
acceptable for dashboard aggregates. The hard entry cap clears the whole
cache before inserting a new key; this keeps the hot path and memory bound
simple when high-cardinality filters churn.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime

TTL_S = 60.0
_MAX_ENTRIES = 1024

_cache: dict[tuple[object, ...], tuple[float, object]] = {}
_lock = threading.Lock()


def make_key(
    shape: str,
    params: dict[str, object],
    from_: datetime,
    to: datetime,
) -> tuple[object, ...]:
    """Return the canonical shape/filter/minute-window cache key."""
    canonical_params = json.dumps(
        sorted((name, value) for name, value in params.items() if value is not None),
        sort_keys=True,
        default=str,
    )
    return (
        shape,
        canonical_params,
        int(from_.timestamp()) // 60,
        int(to.timestamp()) // 60,
    )


def get(key: tuple[object, ...]) -> object | None:
    """Return a live cached result, or ``None`` after a miss or expiry."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        if now - hit[0] >= TTL_S:
            del _cache[key]
            return None
        return hit[1]


def put(key: tuple[object, ...], value: object) -> None:
    """Cache one successful result while preserving the hard entry bound."""
    with _lock:
        if key not in _cache and len(_cache) >= _MAX_ENTRIES:
            _cache.clear()
        _cache[key] = (time.monotonic(), value)


def clear() -> None:
    """Drop all cached results; exposed as a test seam."""
    with _lock:
        _cache.clear()
