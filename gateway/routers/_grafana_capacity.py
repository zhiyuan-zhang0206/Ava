"""Process-local resource budgets exported through Ava's OTLP metrics backend."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Literal

from shared.telemetry_otlp import register_observable_metric

CapacityKind = Literal["http", "sse", "websocket"]

HTTP_LIMIT = 32
SSE_LIMIT = 4
WEBSOCKET_LIMIT = 32

_lock = threading.Lock()
reserved = {"http": 0, "sse": 0, "websocket": 0}
_rejected = {"http": 0, "sse": 0, "websocket": 0}
_limits = {"http": HTTP_LIMIT, "sse": SSE_LIMIT, "websocket": WEBSOCKET_LIMIT}
_snapshot = tuple((kind, 0, limit, 0) for kind, limit in _limits.items())


def _refresh_snapshot() -> None:
    global _snapshot  # noqa: PLW0603 — atomically replace the observer snapshot
    _snapshot = tuple((kind, reserved[kind], _limits[kind], _rejected[kind]) for kind in reserved)


def reserve(kind: CapacityKind, limit: int) -> bool:
    """Atomically reserve one slot, or count and reject without waiting."""
    with _lock:
        _limits[kind] = limit
        if reserved[kind] >= limit:
            _rejected[kind] += 1
            _refresh_snapshot()
            return False
        reserved[kind] += 1
        _refresh_snapshot()
        return True


def release(kind: CapacityKind) -> None:
    """Release exactly one owned slot."""
    with _lock:
        if reserved[kind] <= 0:
            raise RuntimeError(f"grafana {kind} capacity released without reservation")
        reserved[kind] -= 1
        _refresh_snapshot()


def _active_points() -> Iterable[tuple[int, dict[str, str]]]:
    return ((active, {"resource": kind}) for kind, active, _limit, _drops in _snapshot)


def _capacity_points() -> Iterable[tuple[int, dict[str, str]]]:
    return ((limit, {"resource": kind}) for kind, _active, limit, _drops in _snapshot)


def _rejected_points() -> Iterable[tuple[int, dict[str, str]]]:
    return ((drops, {"resource": kind}) for kind, _active, _limit, drops in _snapshot)


def register_metrics() -> None:
    """Register three fixed-cardinality observers once during gateway startup.

    Callbacks only read an immutable tuple. They neither acquire the request
    lock nor emit events, so Grafana querying these metrics cannot create an
    observability feedback loop.
    """
    register_observable_metric(
        "ava_grafana_proxy_capacity_active",
        kind="gauge",
        callback=_active_points,
        description="Active Grafana proxy reservations by resource",
    )
    register_observable_metric(
        "ava_grafana_proxy_capacity_capacity",
        kind="gauge",
        callback=_capacity_points,
        description="Configured Grafana proxy reservation capacity by resource",
    )
    register_observable_metric(
        "ava_grafana_proxy_capacity_rejected",
        kind="counter",
        callback=_rejected_points,
        description="Cumulative Grafana proxy capacity rejections by resource",
    )
