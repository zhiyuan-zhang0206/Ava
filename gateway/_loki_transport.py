"""Loki HTTP transport and availability boundary.

This module owns connection reuse, local read admission, and transport failure
reporting. Query-specific modules build their request shape separately.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from gateway import loki_query_budget
from shared import telemetry
from shared.log import logger
from shared.observability import endpoint_override_is_explicit, gateway_observability_home

_HTTP_TIMEOUT_S = 45.0
_SLOW_QUERY_LOG_S = 5.0


class ObservabilityReadUnavailable(RuntimeError):  # noqa: N818
    """This gateway home has no local observability stack or explicit Loki."""


def _read_gate() -> None:
    """Refuse the default loopback Loki URL outside the observability station."""
    home = gateway_observability_home()
    if home is None:
        return
    from shared.observability import home_is_observability_station

    if not home_is_observability_station(home) and not endpoint_override_is_explicit(
        "AVA_TELEMETRY_LOKI_URL"
    ):
        raise ObservabilityReadUnavailable(
            "observability reads unavailable for this cluster; set "
            "AVA_TELEMETRY_LOKI_URL and provide its stack, or accept that this "
            "cluster has no observability"
        )


def _log_loki_failure(
    *, endpoint: str, params: dict[str, Any], duration_s: float, error: str
) -> None:
    """Emit a structured `loki_query_failed` event for a transport failure.

    The router maps the re-raised exception to a 503; this event is the
    durable record of *which* query shape stalled, for post-mortems when
    Loki's own logs have already rotated away (task #1289: the incident
    window's Loki logs were gone before anyone looked). Best-effort: an
    emit contract violation must never mask the original transport error.
    """
    window_from: str | None = None
    window_to: str | None = None
    start_ns = params.get("start")
    end_ns = params.get("end")
    if isinstance(start_ns, int):
        window_from = datetime.fromtimestamp(start_ns / 1e9, UTC).isoformat()
    if isinstance(end_ns, int):
        window_to = datetime.fromtimestamp(end_ns / 1e9, UTC).isoformat()
    attributes = {
        "endpoint": endpoint,
        "duration_s": round(duration_s, 3),
        "error": error,
        "window_from": window_from,
        "window_to": window_to,
        "query": str(params.get("query", ""))[:500],
    }
    try:
        telemetry.emit("log", "loki_query_failed", level="error", attributes=attributes)
    except Exception:
        logger.error("loki_query_failed emit failed", attributes=attributes)


def _get_json(
    url: str,
    params: dict[str, Any],
    *,
    endpoint: str,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """One Loki HTTP GET with per-call timing and failure reporting.

    Every Loki call in this module funnels through here. Transport failures
    (timeout / disconnect — httpx.TimeoutException / httpx.TransportError)
    and non-2xx responses (httpx.HTTPStatusError) emit a `loki_query_failed`
    event and re-raise untouched; the callers' routers map them to 503s.
    Slow-but-successful queries log one structured line (see
    `_SLOW_QUERY_LOG_S`).
    """
    _read_gate()
    started = time.perf_counter()
    try:
        with loki_query_budget.query_budget.slot():
            if timeout_s is None:
                resp = _client().get(url, params=params)
            else:
                resp = _client().get(url, params=params, timeout=timeout_s)
            resp.raise_for_status()
            payload = resp.json()
    except loki_query_budget.LokiQueryBudgetError:
        # A local admission refusal means the backend was never called. Its
        # own budget transition event/counters already identify queue_full vs
        # acquire_timeout; recording it as loki_query_failed would falsely
        # blame the transport and double-count one failure class.
        raise
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
        duration_s = time.perf_counter() - started
        _log_loki_failure(
            endpoint=endpoint,
            params=params,
            duration_s=duration_s,
            error=type(exc).__name__,
        )
        raise
    duration_s = time.perf_counter() - started
    if duration_s >= _SLOW_QUERY_LOG_S:
        logger.info(
            "loki query slow: {endpoint} {duration_s:.1f}s {query}",
            endpoint=endpoint,
            duration_s=round(duration_s, 1),
            query=str(params.get("query", ""))[:300],
        )
    return payload


def _result_value(series: dict[str, Any]) -> float | None:
    """The instant-query value of one result vector (None when absent)."""
    value = series.get("value") or series.get("values", [None])[-1]
    return float(value[1]) if value else None


# One long-lived HTTP client for every Loki query — connection reuse across
# the gateway's fan-out reads (an inspect poll or ops-monitor call issues
# dozens of queries; a per-call client opened a fresh TCP connection for each).
# The pool ceiling stays above the ops-series peak fan-out (~18 concurrent
# queries) so pool-acquire never times out under normal load.
_shared_client: httpx.Client | None = None


def _client() -> httpx.Client:
    """The module's shared Loki client, created on first use (lazy so tests
    can swap this accessor before any real client exists)."""
    global _shared_client  # noqa: PLW0603 — process-level singleton
    if _shared_client is None:
        _shared_client = httpx.Client(
            timeout=_HTTP_TIMEOUT_S,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=10),
        )
    return _shared_client
