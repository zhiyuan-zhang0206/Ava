"""Per-class warning/error counts for the dashboard's resolution split.

Runs the events-maintenance daemon's grouped query through the gateway's own
Loki budget and caches results like the other dashboard aggregates (task
#3891 B1). Split out of ``_loki_aggregates`` into a focused module when the
result cache pushed that file over the 800-line ceiling.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from gateway import _loki_aggregates, _loki_transport, loki_events_cache
from services.events_maintenance import resolution as _event_resolution
from shared.config import settings


def count_event_classes(
    *,
    from_: datetime,
    to: datetime,
    cluster: str | None = None,
    timeout_s: float | None = None,
) -> dict[_event_resolution.EventClass, int]:
    """Per-class warning/error counts over ``[from_, to]`` — the input to the
    class-resolution arithmetic (task #1935).

    Runs the events-maintenance daemon's grouped query
    (``services.events_maintenance.resolution.grouped_count_query``) through
    the gateway's own Loki budget, so the dashboard's three-way split
    (total / dismissed / net) subtracts exactly the classes the daemon's
    gauges subtract — one query shape, two windows. ``cluster`` scopes the
    raw counts to the current home like every other dashboard Loki read
    (unlabeled pre-labeling rows stay accepted); the dismissal set itself is
    global, so the cancellation matches the daemon either way. Keys are
    ``_event_resolution.EventClass`` values; ``critical`` rows carry their own level
    and fold into the error bucket in the arithmetic, not here.

    Cached like ``count_events`` (task #3891 B1): a minute-floored window key
    plus ``cluster``, 60s TTL, and single-flight concurrent misses; returned
    dicts are copies, so callers can mutate them.
    ``observability.loki_class_cache_enabled`` (default on) switches the cache
    off.

    Transport and budget failures surface exactly like every other gateway
    Loki read (httpx.HTTPError / LokiQueryBudgetError -> 503 by the router).
    """
    if not settings.observability.loki_class_cache_enabled:
        return _count_event_classes_uncached(
            from_=from_, to=to, cluster=cluster, timeout_s=timeout_s
        )
    cache_key = loki_events_cache.make_key("class_counts", {"cluster": cluster}, from_, to)
    cached = loki_events_cache.get(cache_key)
    if isinstance(cached, dict):
        return dict(cast(dict[_event_resolution.EventClass, int], cached))
    holder, is_leader = loki_events_cache.begin(cache_key)
    if is_leader:
        cached = loki_events_cache.get(cache_key)
        if isinstance(cached, dict):
            cached_counts = cast(dict[_event_resolution.EventClass, int], cached)
            loki_events_cache.finish(cache_key, holder, value=cached_counts)
            return dict(cached_counts)
    else:
        holder.event.wait(loki_events_cache._INFLIGHT_WAIT_S)
        if holder.error is not None:
            raise holder.error
        inflight_value = cast(dict[_event_resolution.EventClass, int] | None, holder.value)
        if isinstance(inflight_value, dict):
            return dict(inflight_value)
    try:
        counts = _count_event_classes_uncached(
            from_=from_, to=to, cluster=cluster, timeout_s=timeout_s
        )
        stored = dict(counts)
        loki_events_cache.put(cache_key, stored)
    except BaseException as exc:
        if is_leader:
            loki_events_cache.finish(cache_key, holder, error=exc)
        raise
    if is_leader:
        loki_events_cache.finish(cache_key, holder, value=stored)
    return counts


def _count_event_classes_uncached(
    *,
    from_: datetime,
    to: datetime,
    cluster: str | None,
    timeout_s: float | None,
) -> dict[_event_resolution.EventClass, int]:
    """One uncached pass of the daemon-shaped class query."""
    window_s = int((to - from_).total_seconds())
    logql = _event_resolution.grouped_count_query(f"{window_s}s", cluster=cluster)
    counts: dict[_event_resolution.EventClass, int] = {}
    for series in _loki_aggregates._query_instant(logql, to, timeout_s=timeout_s):
        value = _loki_transport._result_value(series)
        if value is None:
            continue
        metric = series.get("metric", {})
        event_class = _event_resolution.EventClass(
            category=str(metric.get("category", "")),
            level=str(metric.get("level", "")),
            event_name=str(metric.get("event_name", "")),
            source=str(metric.get("source", "")),
            process=str(metric.get("process", "")),
        )
        counts[event_class] = counts.get(event_class, 0) + int(value)
    return counts
