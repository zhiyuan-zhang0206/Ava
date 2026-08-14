"""Prometheus-backed telemetry aggregates — the LGTM read side (task #1197).

The emitter (`shared/telemetry_otlp._record_metrics`) maps every telemetry
event's numeric payload fields to OTLP instruments: int -> Counter named
`ava_<event_name>_<field>` (Prometheus appends `_total` to counters), float
-> Histogram. This module is the read side: instant queries over
Prometheus's HTTP API (`/api/v1/query`) for the llm_usage aggregates the
gateway surfaces (fleet graph node totals/scores, dashboard token sums).

Notes:
- Datapoint attributes carry machine / process / agent_id plus short-string
  payload fields (model, ok, ...), so `sum by (agent_id) (...)` and
  `sum by (model) (...)` work directly on the mapped counters.
- Counters are cumulative since the exporting process started. The all-time
  view reads the raw counter; the windowed view wraps it in
  `increase(...[Nh])` — the counter delta over the trailing N hours, the
  PromQL equivalent of the old SQL `SUM(field) WHERE ts > now() - N hours`.
- Only telemetry-category events produce metrics (log/audit do not), which
  matches the old SQL's `category IN ('telemetry', 'log')` filter for
  llm_usage (a telemetry event).
"""

from __future__ import annotations

import math
from datetime import datetime

import httpx

from shared.config import settings

# Instant queries over the llm_usage counters are fast; the timeout is the
# same generous bound the Loki read path uses.
_HTTP_TIMEOUT_S = 60.0


def query(expr: str) -> list[tuple[dict[str, str], float]]:
    """Run one instant query; return the `data.result` as (labels, value)
    pairs in API order.

    Raises on transport errors and non-2xx responses (the caller decides
    whether to degrade); a `data.result` missing or empty yields [].
    """
    url = settings.observability.telemetry_prometheus_url.rstrip("/") + "/api/v1/query"
    params = {"query": expr}
    with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        payload = resp.json()

    rows: list[tuple[dict[str, str], float]] = []
    for item in payload.get("data", {}).get("result", []):
        value = item.get("value")
        if not value or len(value) < 2:
            continue
        labels: dict[str, str] = {}
        metric = item.get("metric")
        if isinstance(metric, dict):
            for k, v in metric.items():
                if isinstance(k, str) and isinstance(v, str):
                    labels[k] = v
        rows.append((labels, float(value[1])))
    return rows


def sum_by(metric: str, by: str, *, window_hours: int | None = None) -> dict[str, float]:
    """Aggregate a counter metric grouped by one label: `sum by (<by>) (<metric>)`.

    `window_hours=None` reads the raw cumulative counter (all-time since the
    exporting process started); a window wraps it in `increase(...[Nh])` —
    the counter delta over the trailing N hours, matching the old SQL's
    `ts > now() - N hours` window. Returns `{label value: sum}`; series
    missing the label group under `""`.
    """
    if window_hours is None:
        expr = f"sum by ({by}) ({metric})"
    else:
        expr = f"sum by ({by}) (increase({metric}[{int(window_hours)}h]))"
    out: dict[str, float] = {}
    for labels, value in query(expr):
        key = labels.get(by, "")
        out[key] = out.get(key, 0.0) + value
    return out


def query_range(
    expr: str, *, start: datetime, end: datetime, step_s: int
) -> list[tuple[dict[str, str], list[tuple[int, float]]]]:
    """One Prometheus range query (`/api/v1/query_range`) at a fixed step.

    Returns per-series `(labels, [(ts_s, value), ...])` in API order. Steps
    without samples are absent (callers zero-fill against their own bucket
    grid); NaN values (e.g. `histogram_quantile` over an empty window) are
    dropped. Prometheus emits exactly the requested step timestamps, so
    series align to the caller's grid.
    """
    url = settings.observability.telemetry_prometheus_url.rstrip("/") + "/api/v1/query_range"
    params = {
        "query": expr,
        "start": start.timestamp(),
        "end": end.timestamp(),
        "step": f"{int(step_s)}s",
    }
    with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        payload = resp.json()

    rows: list[tuple[dict[str, str], list[tuple[int, float]]]] = []
    for item in payload.get("data", {}).get("result", []):
        labels: dict[str, str] = {}
        metric = item.get("metric")
        if isinstance(metric, dict):
            for k, v in metric.items():
                if isinstance(k, str) and isinstance(v, str):
                    labels[k] = v
        values: list[tuple[int, float]] = []
        for ts, value in item.get("values", []):
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if math.isnan(v):
                continue
            values.append((int(float(ts)), v))
        if values:
            rows.append((labels, values))
    return rows
