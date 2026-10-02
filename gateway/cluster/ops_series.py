"""Ops series — the query core behind `GET /api/ops/monitor` (the Insights Ops panel).

Same report shape and the same fixed bucket grid as always (`OPS_GRID_ORIGIN`-aligned; 1h→60s,
6h→300s, 24h→1800s, 7d→3600s, zero-filled). Every metric group is computed from the
`telemetry_events` rows of the window, in one pooled connection, one statement per group:

- **sse** — `sse_drop` counted by payload `kind` (`queue_full` vs everything else; rows without a
  kind count toward neither) + `event_log_drop`, per bucket.
- **llm** — from the `llm_usage` rows: calls, tokens, latency sum, exact p50 / p95 / max latency
  per bucket (and over the whole window), plus the LLM error family events per bucket.
- **restarts** — `agent_restarted` + `service_started` per bucket, plus whole-window breakdowns:
  services by `attributes.name` (count and last start), agents by `agent_id` (top 20; labels from
  the `agents` registry).

A bucket is half-open, `[start, start + bucket)`, as `meta.bucket_starts` says; the in-progress
bucket is partial. Counts and sums are exact; percentiles are exact over the rows of the bucket
(the retired Prometheus reader approximated them from a latency histogram).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, LiteralString, cast

import psycopg

from base.events.contract import (
    LLM_ERROR_FAMILY,
    LLM_USAGE_KEYS,
    OPS_GRID_ORIGIN,
    SERVICE_STARTED_KEYS,
    SSE_DROP_KEYS,
    family_events,
)
from base.telemetry.event_sql import numeric

# Window -> (seconds, bucket seconds). Bucket count per window is fixed:
# 1h=60, 6h=72, 24h=48, 7d=168 points — enough shape, small payloads.
WINDOWS: dict[str, tuple[int, int]] = {
    "1h": (3600, 60),
    "6h": (21600, 300),
    "24h": (86400, 1800),
    "7d": (604800, 3600),
}

_GRID_ORIGIN = OPS_GRID_ORIGIN
_LLM_ERROR_EVENTS = list(family_events(LLM_ERROR_FAMILY))


def _bucket_starts(anchor: datetime, window_s: int, bucket_s: int) -> list[datetime]:
    """Bucket boundary times covering `[anchor - window, anchor]`, oldest
    first, on the fixed 60s grid. The last bucket is the in-progress one
    (start <= anchor < start + bucket_s), so the panel's live-most point is
    always present; the series arrays are indexed by position in this list."""
    n = window_s // bucket_s
    elapsed = int((anchor - _GRID_ORIGIN).total_seconds())
    last = _GRID_ORIGIN + timedelta(seconds=elapsed - (elapsed % bucket_s))
    return [last - timedelta(seconds=(n - 1 - i) * bucket_s) for i in range(n)]


def _round1(v: float | None) -> float | None:
    return round(v, 1) if v is not None else None


def _execute(
    conn: psycopg.Connection[Any], query: str, params: tuple[Any, ...]
) -> psycopg.Cursor[Any]:
    """Run a query assembled from integers and the registered payload-key constants."""
    return conn.execute(cast(LiteralString, query), params)


def _bucket(start_s: int, bucket_s: int) -> str:
    """The SQL expression of an event's bucket index, counted from the first bucket."""
    return f"floor((extract(epoch FROM ts) - {int(start_s)}) / {int(bucket_s)})::int"


class _Window:
    """The bucket grid of one request and the query parameters that bound it."""

    def __init__(self, bucket_starts: list[datetime], bucket_s: int) -> None:
        self.bucket_s = bucket_s
        self.n = len(bucket_starts)
        self.start = bucket_starts[0]
        self.end = bucket_starts[-1] + timedelta(seconds=bucket_s)
        self.bucket_sql = _bucket(int(self.start.timestamp()), bucket_s)

    def zeros(self) -> list[int]:
        return [0] * self.n

    def fill(self, rows: dict[int, Any], default: Any) -> list[Any]:
        return [rows.get(i, default) for i in range(self.n)]


def _sse_series(conn: psycopg.Connection[Any], window: _Window) -> dict[str, Any]:
    kind = f"COALESCE({SSE_DROP_KEYS['kind']}, '')"
    rows = _execute(
        conn,
        f"""
        SELECT {window.bucket_sql} AS bkt,
               count(*) FILTER (WHERE event_name = 'sse_drop' AND {kind} = 'queue_full'),
               count(*) FILTER (WHERE event_name = 'sse_drop'
                                AND {kind} NOT IN ('', 'queue_full')),
               count(*) FILTER (WHERE event_name = 'event_log_drop')
        FROM telemetry_events
        WHERE event_name IN ('sse_drop', 'event_log_drop') AND ts >= %s AND ts < %s
        GROUP BY bkt
        """,  # noqa: S608 — keys come from the registered payload constants
        (window.start, window.end),
    ).fetchall()
    by_bucket = {int(r[0]): (int(r[1]), int(r[2]), int(r[3])) for r in rows}
    cells = window.fill(by_bucket, (0, 0, 0))
    queue_full, publish_error, event_log_drop = (
        list(column) for column in zip(*cells, strict=True)
    )
    return {
        "series": [
            {"bucket": i, "queue_full": q, "publish_error": p, "event_log_drop": e}
            for i, (q, p, e) in enumerate(
                zip(queue_full, publish_error, event_log_drop, strict=True)
            )
        ],
        "totals": {
            "queue_full": sum(queue_full),
            "publish_error": sum(publish_error),
            "event_log_drop": sum(event_log_drop),
        },
    }


def _llm_series(conn: psycopg.Connection[Any], window: _Window) -> dict[str, Any]:
    latency = numeric(LLM_USAGE_KEYS["latency_ms"])
    usage = "event_name = 'llm_usage'"
    rows = _execute(
        conn,
        f"""
        SELECT bkt,
               count(*) FILTER (WHERE {usage}),
               COALESCE(sum(tin), 0), COALESCE(sum(tout), 0), COALESCE(sum(treason), 0),
               COALESCE(sum(lat), 0), max(lat),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY lat),
               percentile_cont(0.95) WITHIN GROUP (ORDER BY lat),
               count(*) FILTER (WHERE event_name = ANY(%s)),
               grouping(bkt)
        FROM (
          SELECT {window.bucket_sql} AS bkt, event_name,
                 CASE WHEN {usage} THEN {numeric(LLM_USAGE_KEYS["in_total"])} END AS tin,
                 CASE WHEN {usage} THEN {numeric(LLM_USAGE_KEYS["out_total"])} END AS tout,
                 CASE WHEN {usage} THEN {numeric(LLM_USAGE_KEYS["reasoning"])} END AS treason,
                 CASE WHEN {usage} THEN {latency} END AS lat
          FROM telemetry_events
          WHERE (event_name = 'llm_usage' OR event_name = ANY(%s)) AND ts >= %s AND ts < %s
        ) s
        GROUP BY GROUPING SETS ((bkt), ())
        """,  # noqa: S608 — keys come from the registered payload constants
        (_LLM_ERROR_EVENTS, _LLM_ERROR_EVENTS, window.start, window.end),
    ).fetchall()
    per_bucket: dict[int, tuple[Any, ...]] = {}
    total: tuple[Any, ...] | None = None
    for row in rows:
        if row[10]:
            total = row
        else:
            per_bucket[int(row[0])] = row
    empty: tuple[Any, ...] = (0, 0, 0, 0, 0, 0, None, None, None, 0, 0)

    def column(index: int, *, count: bool = False) -> list[Any]:
        values = [per_bucket.get(i, empty)[index] for i in range(window.n)]
        return [round(v) if count and v is not None else v for v in values]

    calls = [int(v) for v in column(1)]
    tokens_in = column(2, count=True)
    tokens_out = column(3, count=True)
    tokens_reasoning = column(4, count=True)
    lat_sum = column(5, count=True)
    lat_max = column(6)
    p50 = column(7)
    p95 = column(8)
    errors = [int(v) for v in column(9)]

    series: list[dict[str, Any]] = []
    for i in range(window.n):
        tps = None
        if lat_sum[i]:
            tps = _round1(
                (tokens_in[i] + tokens_out[i] + tokens_reasoning[i]) / (lat_sum[i] / 1000.0)
            )
        series.append(
            {
                "bucket": i,
                "calls": calls[i],
                "latency_p50_ms": _round1(p50[i]),
                "latency_p95_ms": _round1(p95[i]),
                "latency_max_ms": _round1(lat_max[i]),
                "tokens_in": tokens_in[i],
                "tokens_out": tokens_out[i],
                "tps": tps,
                "errors": errors[i],
            }
        )
    t_tokens = sum(tokens_in) + sum(tokens_out) + sum(tokens_reasoning)
    t_lat = sum(lat_sum)
    return {
        "series": series,
        "totals": {
            "calls": sum(calls),
            "latency_p50_ms": _round1(total[7]) if total else None,
            "latency_p95_ms": _round1(total[8]) if total else None,
            "latency_max_ms": _round1(total[6]) if total else None,
            "tokens_in": sum(tokens_in),
            "tokens_out": sum(tokens_out),
            "tps": _round1(t_tokens / (t_lat / 1000.0)) if t_lat else None,
            "errors": sum(errors),
        },
    }


def _restart_series(
    conn: psycopg.Connection[Any],
    window: _Window,
    label_lookup: Callable[[list[int]], dict[int, str | None]] | None,
) -> dict[str, Any]:
    rows = _execute(
        conn,
        f"""
        SELECT {window.bucket_sql} AS bkt,
               count(*) FILTER (WHERE event_name = 'agent_restarted'),
               count(*) FILTER (WHERE event_name = 'service_started')
        FROM telemetry_events
        WHERE event_name IN ('agent_restarted', 'service_started') AND ts >= %s AND ts < %s
        GROUP BY bkt
        """,  # noqa: S608 — the bucket expression is built from integers
        (window.start, window.end),
    ).fetchall()
    cells = window.fill({int(r[0]): (int(r[1]), int(r[2])) for r in rows}, (0, 0))
    agent_restarts, service_starts = (list(column) for column in zip(*cells, strict=True))

    name = f"btrim(COALESCE({SERVICE_STARTED_KEYS['name']}, ''))"
    services: list[dict[str, Any]] = [
        {"name": r[0], "starts": int(r[1]), "last_start": r[2].astimezone(UTC).isoformat()}
        for r in _execute(
            conn,
            f"""
            SELECT {name} AS service, count(*), max(ts)
            FROM telemetry_events
            WHERE event_name = 'service_started' AND ts >= %s AND ts < %s AND {name} <> ''
            GROUP BY service ORDER BY count(*) DESC, service
            """,  # noqa: S608 — keys come from the registered payload constants
            (window.start, window.end),
        ).fetchall()
    ]
    top = [
        (int(r[0]), int(r[1]))
        for r in conn.execute(
            """
            SELECT agent_id, count(*) FROM telemetry_events
            WHERE event_name = 'agent_restarted' AND agent_id IS NOT NULL
              AND ts >= %s AND ts < %s
            GROUP BY agent_id ORDER BY count(*) DESC, agent_id LIMIT 20
            """,
            (window.start, window.end),
        ).fetchall()
    ]
    labels: dict[int, str | None] = {}
    if top and label_lookup is not None:
        labels = label_lookup([aid for aid, _ in top])
    agents = [{"agent_id": aid, "label": labels.get(aid), "restarts": c} for aid, c in top]
    return {
        "series": [
            {"bucket": i, "agent_restarts": a, "service_starts": s}
            for i, (a, s) in enumerate(zip(agent_restarts, service_starts, strict=True))
        ],
        "services": services,
        "agents": agents,
        "totals": {"agent_restarts": sum(agent_restarts), "service_starts": sum(service_starts)},
    }


def fetch_ops_series(
    conn: psycopg.Connection[Any],
    window: str,
    *,
    label_lookup: Callable[[list[int]], dict[int, str | None]] | None = None,
) -> dict[str, Any]:
    """Run every registered ops series for `window` and return the full report
    dict — meta + the three groups — ready for `OpsMonitorReport(**data)`.

    `label_lookup`, when supplied, resolves the agents-registry labels of the restart
    breakdown.
    """
    window_s, bucket_s = WINDOWS[window]
    anchor = datetime.now(UTC)
    bucket_starts = _bucket_starts(anchor, window_s, bucket_s)
    grid = _Window(bucket_starts, bucket_s)
    return {
        "meta": {
            "window": window,
            "bucket_seconds": bucket_s,
            "generated_at": anchor.isoformat(),
            "bucket_starts": [b.isoformat() for b in bucket_starts],
        },
        "sse": _sse_series(conn, grid),
        "llm": _llm_series(conn, grid),
        "restarts": _restart_series(conn, grid, label_lookup),
    }
