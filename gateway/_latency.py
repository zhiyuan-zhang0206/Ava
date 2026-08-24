"""Per-endpoint gateway latency metering (Task #1091).

A middleware times every HTTP request and accumulates durations in memory,
keyed by the **matched route pattern** (``/api/agents/{agent_id}/messages``,
not the concrete URL — ids in URLs would explode the key space). A lifespan
background task drains the accumulator every 60s and emits one
``gateway_latency`` telemetry event per (route, bucket) carrying the bucket's
p50 / p95 / p99 / max / count.

Why aggregate instead of one event per request: the ``events`` stream is the
cheapest place for the ops dashboard to read, but per-request rows at gateway
volume (health probes alone are 4 hosts x ~1/s) would bury everything else.
60s x route bounds the row rate to (distinct routes) rows/minute regardless of
request rate.

Both the middleware and the flusher run on the gateway's single event loop, so
the accumulator needs no locking: the middleware only appends between awaits,
and the flusher swaps the whole dict out in one step (``drain``).

Latency measured is **time to response headers** — ``await call_next()``
returns when the response starts, so a long-lived SSE connection counts its
time-to-first-byte, not its lifetime. Exceptions still record the elapsed time
(the ``finally`` runs before the exception propagates).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from shared import telemetry

_log = logging.getLogger(__name__)

# One aggregate per route per bucket — the row rate is bounded by the number
# of distinct routes, never by request volume.
FLUSH_INTERVAL_S = 60.0

# Safety valve: if one route bursts beyond this many samples in a bucket
# (e.g. a retry storm), keep the first samples and drop the rest. 10k floats
# ≈ 80 KiB per route — the accumulator stays trivially small even under an
# attack-scale burst.
MAX_SAMPLES_PER_ROUTE = 10_000

# R17/R18 exclude these matched route patterns. Keep the alerting policy here,
# beside the emitted route value, so a new slow route cannot drift from one
# alert tier while remaining visible on the all-route dashboard.
R17_R18_EXCLUSION_ROUTE_PATTERNS: dict[str, tuple[str, ...]] = {
    "llm": (
        r"/api/agents/.*/messages",
        r"/api/agents/.*/shell/.*",
        r"/api/agents/.*/traces/.*",
    ),
    "slow": (
        r"/api/memory/search",
        r"/api/agents/.*/inspect",
        r"/api/stats/dashboard",
        r"/api/metrics/agents",
        r"/api/events",
        r"/api/fleet/graph",
        r"/api/agents/.*/terminate",
        r"/api/agents/.*/resurrect",
    ),
}
_ROUTE_CLASS_REGEXES = {
    route_class: tuple(re.compile(f"^{pattern}$") for pattern in patterns)
    for route_class, patterns in R17_R18_EXCLUSION_ROUTE_PATTERNS.items()
}

# Unmatched requests (404s, scanners) would otherwise open an unbounded key
# space per raw path; beyond this many distinct keys everything unmatched
# folds into one shared bucket.
MAX_DISTINCT_ROUTES = 1_000

# ── accumulator ────────────────────────────────────────────────────────────

_accumulator: dict[str, list[float]] = {}


def _route_pattern(request: Request) -> str:
    """The matched route pattern for `request`, else its raw path.

    FastAPI/Starlette sets ``scope["route"]`` while the router runs — which
    happens inside ``call_next`` — so this must be called *after* the
    middleware has awaited it, never before.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return request.url.path


def record(route: str, duration_ms: float) -> None:
    """Append one sample to the accumulator (event-loop thread only)."""
    samples = _accumulator.get(route)
    if samples is None:
        if len(_accumulator) >= MAX_DISTINCT_ROUTES and route not in _accumulator:
            route = "_unmatched:other"
            samples = _accumulator.get(route)
        if samples is None:
            samples = []
            _accumulator[route] = samples
    if len(samples) < MAX_SAMPLES_PER_ROUTE:
        samples.append(duration_ms)


def classify_route(route: str) -> str:
    """Classify one matched route for latency-alert eligibility."""
    for route_class, patterns in _ROUTE_CLASS_REGEXES.items():
        if any(pattern.fullmatch(route) for pattern in patterns):
            return route_class
    return "fast"


def drain() -> dict[str, list[float]]:
    """Take a snapshot of the accumulator and clear it in one step.

    Runs on the event loop like ``record``, with no ``await`` in between, so a
    request cannot interleave: the snapshot is the whole bucket and the next
    request starts a fresh one.
    """
    batch = dict(_accumulator)
    _accumulator.clear()
    return batch


def percentiles(durations_ms: list[float], *ps: float) -> list[float]:
    """Nearest-rank percentiles of `durations_ms` (0.0 for an empty bucket).

    The nearest-rank method is what Grafana's own histogram math uses for
    p-values — deterministic and stable on tiny samples, unlike linear
    interpolation.
    """
    if not durations_ms:
        return [0.0 for _ in ps]
    ordered = sorted(durations_ms)
    n = len(ordered)
    out: list[float] = []
    for p in ps:
        idx = min(n - 1, max(0, math.ceil(p / 100.0 * n) - 1))
        out.append(ordered[idx])
    return out


def emit_bucket(route: str, samples: list[float]) -> None:
    """Compute the bucket stats for one route and emit the aggregate event.

    Exposed separately from the flusher loop so tests can drive it directly.
    """
    if not samples:
        return
    p50, p95, p99, max_ms = percentiles(samples, 50.0, 95.0, 99.0, 100.0)
    telemetry.emit(
        "telemetry",
        "gateway_latency",
        attributes={
            "route": route,
            "route_class": classify_route(route),
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "p99_ms": round(p99, 1),
            "max_ms": round(max_ms, 1),
            "count": len(samples),
        },
    )


async def latency_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Time the request and record it under its matched route pattern."""
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        record(_route_pattern(request), (time.perf_counter() - start) * 1000.0)
    return response


async def latency_flusher() -> None:
    """Drain the accumulator every `FLUSH_INTERVAL_S` and emit aggregates.

    Runs as a lifespan background task. A failed flush never kills the loop —
    the next tick retries (a dropped bucket is only a monitoring gap, and the
    emit pipeline itself is already best-effort).
    """
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_S)
        try:
            batch = drain()
            for route, samples in batch.items():
                emit_bucket(route, samples)
        except Exception:
            _log.warning("gateway latency flush failed", exc_info=True)
