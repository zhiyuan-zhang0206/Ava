"""Short result cache for repeated Loki dashboard aggregations.

Dashboard clients poll every 30 seconds and re-issue identical aggregation
shapes. Flooring both window bounds to minute identifiers makes those poll
keys stable, while also deduplicating shared clock-aligned shards across
different windows. A result can therefore be up to 60 seconds stale, which is
acceptable for dashboard aggregates. The entry cap evicts the
least-recently-used key when the cache is full (``AVA_LOKI_EVENTS_CACHE_MAX_ENTRIES``,
default 1024), so memory stays bounded while high-cardinality filters churn.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime

from shared.config import settings

# 60s matches the dashboard poll rhythm (a second 30s poll reuses the first
# result) and the panel is a non-alerting read path, so up-to-60s staleness is
# acceptable for these aggregates (task #3891).
TTL_S = 60.0
_INFLIGHT_WAIT_S = 60.0


@dataclass
class _Inflight:
    event: threading.Event = field(default_factory=threading.Event)
    value: object | None = None
    error: BaseException | None = None


# Recency-ordered: `get()` refreshes a hit's position, `put()` appends, and a
# full cache evicts from the front — the least recently used entry.
_cache: OrderedDict[tuple[object, ...], tuple[float, object]] = OrderedDict()
_inflight: dict[tuple[object, ...], _Inflight] = {}
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
        _cache.move_to_end(key)
        return hit[1]


def put(key: tuple[object, ...], value: object) -> None:
    """Cache one successful result, evicting the least recently used entry
    while the configured entry cap is exceeded."""
    max_entries = settings.observability.loki_events_cache_max_entries
    with _lock:
        _cache[key] = (time.monotonic(), value)
        _cache.move_to_end(key)
        while len(_cache) > max_entries:
            _cache.popitem(last=False)


def begin(key: tuple[object, ...]) -> tuple[_Inflight, bool]:
    """Return the key's in-flight holder and whether this caller created it."""
    with _lock:
        holder = _inflight.get(key)
        if holder is not None:
            return holder, False
        holder = _Inflight()
        _inflight[key] = holder
        return holder, True


def finish(
    key: tuple[object, ...],
    holder: _Inflight,
    *,
    value: object | None = None,
    error: BaseException | None = None,
) -> None:
    """Publish one computation's outcome and detach its registry entry."""
    with _lock:
        holder.value = value
        holder.error = error
        if _inflight.get(key) is holder:
            del _inflight[key]
    holder.event.set()


def clear() -> None:
    """Drop cached results and detach in-flight holders without stranding waiters."""
    with _lock:
        _cache.clear()
        _inflight.clear()
