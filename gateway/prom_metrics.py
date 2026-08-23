"""Prometheus-backed telemetry aggregates — the LGTM read side (task #1197)."""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime
from typing import Any, cast

import httpx

from gateway.loki_query_budget import (
    BudgetObservation,
    BudgetObserver,
    BudgetRejectReason,
    FairQueryBudget,
)
from shared import telemetry
from shared.config import settings
from shared.log import logger

# Instant queries over the llm_usage counters are fast; the timeout is the
# same generous bound the Loki read path uses.
_HTTP_TIMEOUT_S = 60.0
_PROM_QUERY_CONCURRENCY = 2
_PROM_QUERY_MAX_WAITERS = 128
_PROM_QUERY_WAIT_TIMEOUT_S = 10.0


class PromQueryBudgetError(httpx.PoolTimeout):
    """A local Prometheus-capacity refusal with a stable reason."""

    def __init__(self, reason: BudgetRejectReason) -> None:
        message = (
            "Prometheus query queue is full"
            if reason == "queue_full"
            else "timed out waiting for the Prometheus query budget"
        )
        super().__init__(message)
        self.reason = reason


def _emit_budget_observation(observation: BudgetObservation) -> None:
    """Emit Prometheus admission telemetry without touching its HTTP client."""
    telemetry.emit(
        "telemetry",
        "prom_query_budget",
        attributes={
            "outcome": observation.outcome,
            "active": observation.active,
            "queued": observation.queued,
            "high_water": observation.high_water,
            "wait_ms": observation.wait_ms,
            "acquired": observation.acquired,
            "queue_full": observation.queue_full,
            "wait_timeout": observation.wait_timeout,
        },
    )


prom_query_budget = FairQueryBudget(
    capacity=_PROM_QUERY_CONCURRENCY,
    max_waiters=_PROM_QUERY_MAX_WAITERS,
    wait_timeout_s=_PROM_QUERY_WAIT_TIMEOUT_S,
    observer=_emit_budget_observation,
    error_factory=PromQueryBudgetError,
)


_consecutive_prom_failures = 0
_consecutive_prom_failures_lock = threading.Lock()


def _reset_consecutive_prom_failures() -> None:
    global _consecutive_prom_failures  # noqa: PLW0603 — process-local failure streak
    with _consecutive_prom_failures_lock:
        _consecutive_prom_failures = 0


def _record_consecutive_prom_failure() -> int:
    global _consecutive_prom_failures  # noqa: PLW0603 — process-local failure streak
    with _consecutive_prom_failures_lock:
        _consecutive_prom_failures += 1
        return _consecutive_prom_failures


def reset_for_tests(
    *,
    capacity: int = _PROM_QUERY_CONCURRENCY,
    max_waiters: int = _PROM_QUERY_MAX_WAITERS,
    wait_timeout_s: float = _PROM_QUERY_WAIT_TIMEOUT_S,
    observer: BudgetObserver | None = None,
) -> None:
    """Replace the process-wide Prometheus budget between isolated tests."""
    global prom_query_budget  # noqa: PLW0603 — intentional singleton test seam
    _reset_consecutive_prom_failures()
    prom_query_budget = FairQueryBudget(
        capacity=capacity,
        max_waiters=max_waiters,
        wait_timeout_s=wait_timeout_s,
        observer=observer,
        error_factory=PromQueryBudgetError,
    )


# One long-lived HTTP client for every Prometheus query — connection reuse
# across the dashboard/ops fan-outs (same rationale and sizing as the shared
# Loki client in `gateway/loki_events.py`).
_shared_client: httpx.Client | None = None


def _client() -> httpx.Client:
    """The module's shared Prometheus client, created lazily for test seams."""
    global _shared_client  # noqa: PLW0603 — process-level singleton
    if _shared_client is None:
        _shared_client = httpx.Client(
            timeout=_HTTP_TIMEOUT_S,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=10),
        )
    return _shared_client


def _log_prom_failure(*, endpoint: str, duration_s: float, error: str, query: str) -> None:
    """Record one Prometheus transport failure without masking the original."""
    failures = _record_consecutive_prom_failure()
    if failures != 1 and failures % 50 != 0:
        return
    attributes = {
        "endpoint": endpoint,
        "duration_s": round(duration_s, 3),
        "error": error,
        "query": query[:500],
    }
    try:
        telemetry.emit("log", "prom_query_failed", level="error", attributes=attributes)
    except Exception:
        logger.error("prom_query_failed emit failed", attributes=attributes)


def _get_json(
    url: str,
    params: dict[str, Any],
    *,
    endpoint: str,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """One budgeted Prometheus GET with durable transport-failure telemetry."""
    started = time.perf_counter()
    try:
        with prom_query_budget.slot():
            if timeout_s is None:
                response = _client().get(url, params=params)
            else:
                response = _client().get(url, params=params, timeout=timeout_s)
            response.raise_for_status()
            _reset_consecutive_prom_failures()
            return response.json()
    except PromQueryBudgetError:
        # The local admission transition already records its rejection. Do not
        # misattribute it to the transport or double-count it as a query failure.
        raise
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
        _log_prom_failure(
            endpoint=endpoint,
            duration_s=time.perf_counter() - started,
            error=type(exc).__name__,
            query=str(params.get("query", "")),
        )
        raise


def _json_object(value: Any) -> dict[str, Any] | None:
    """Return a typed JSON object, rejecting scalar and array payloads."""
    if not isinstance(value, dict):
        return None
    return cast(dict[str, Any], value)


def _json_array(value: Any) -> list[Any] | None:
    """Return a typed JSON array, rejecting scalar and object payloads."""
    if not isinstance(value, list):
        return None
    return cast(list[Any], value)


def _json_sequence(value: Any) -> list[Any] | tuple[Any, ...] | None:
    """Return a typed JSON-like positional value, accepting test tuples too."""
    if not isinstance(value, (list, tuple)):
        return None
    return cast(list[Any] | tuple[Any, ...], value)


def query(
    expr: str,
    *,
    timeout_s: float | None = None,
) -> list[tuple[dict[str, str], float]]:
    """Run one instant query; return the `data.result` as (labels, value) pairs."""
    url = settings.observability.telemetry_prometheus_url.rstrip("/") + "/api/v1/query"
    payload = _get_json(url, {"query": expr}, endpoint="query", timeout_s=timeout_s)

    rows: list[tuple[dict[str, str], float]] = []
    data = _json_object(payload.get("data"))
    if data is None:
        return rows
    result = data.get("result", [])
    if not isinstance(result, list):
        return rows
    for raw_item in result:
        item = _json_object(raw_item)
        if item is None:
            continue
        value = _json_array(item.get("value"))
        if value is None or len(value) < 2:
            continue
        labels: dict[str, str] = {}
        metric = item.get("metric")
        if isinstance(metric, dict):
            for key, label in metric.items():
                if isinstance(key, str) and isinstance(label, str):
                    labels[key] = label
        rows.append((labels, float(value[1])))
    return rows


def sum_by(
    metric: str,
    by: str,
    *,
    window_hours: int | None = None,
    timeout_s: float | None = None,
) -> dict[str, float]:
    """Aggregate a counter metric grouped by one label."""
    if window_hours is None:
        expr = f"sum by ({by}) ({metric})"
    else:
        expr = f"sum by ({by}) (increase({metric}[{int(window_hours)}h]))"
    out: dict[str, float] = {}
    for labels, value in query(expr, timeout_s=timeout_s):
        key = labels.get(by, "")
        out[key] = out.get(key, 0.0) + value
    return out


def query_range(
    expr: str,
    *,
    start: datetime,
    end: datetime,
    step_s: int,
    timeout_s: float | None = None,
) -> list[tuple[dict[str, str], list[tuple[int, float]]]]:
    """One Prometheus range query (`/api/v1/query_range`) at a fixed step."""
    url = settings.observability.telemetry_prometheus_url.rstrip("/") + "/api/v1/query_range"
    payload = _get_json(
        url,
        {
            "query": expr,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": f"{int(step_s)}s",
        },
        endpoint="query_range",
        timeout_s=timeout_s,
    )

    rows: list[tuple[dict[str, str], list[tuple[int, float]]]] = []
    data = _json_object(payload.get("data"))
    if data is None:
        return rows
    result = data.get("result", [])
    if not isinstance(result, list):
        return rows
    for raw_item in result:
        item = _json_object(raw_item)
        if item is None:
            continue
        labels: dict[str, str] = {}
        metric = item.get("metric")
        if isinstance(metric, dict):
            for key, label in metric.items():
                if isinstance(key, str) and isinstance(label, str):
                    labels[key] = label
        values: list[tuple[int, float]] = []
        raw_values = _json_array(item.get("values", []))
        if raw_values is None:
            continue
        for raw_value in raw_values:
            value_pair = _json_sequence(raw_value)
            if value_pair is None or len(value_pair) < 2:
                continue
            try:
                value = float(value_pair[1])
            except (TypeError, ValueError):
                continue
            if math.isnan(value):
                continue
            values.append((int(float(value_pair[0])), value))
        if values:
            rows.append((labels, values))
    return rows
