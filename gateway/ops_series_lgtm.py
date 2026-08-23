"""LGTM-backed ops series — the query core behind `GET /api/ops/monitor`
(the Insights Ops panel), task #1197.

Replaces `shared/ops_series` (the PG `ops_metrics` read path, retired with
the ops-monitor migration): same report shape and the same fixed bucket grid
(`OPS_GRID_ORIGIN`-aligned; 1h→60s, 6h→300s, 24h→1800s, 7d→3600s,
zero-filled), but the three metric groups now come from Loki + Prometheus
instead of the pre-aggregated `ops_metrics` table:

- **sse** — `sse_drop` (agent SSE publisher shed) counted by payload `kind`
  (`queue_full` vs everything else; rows without a kind count toward neither,
  matching the old FILTER semantics) + `event_log_drop` (the DB log sink
  shed), both from Loki.
- **llm** — calls / tokens / latency sum / p50 / p95 from the Prometheus
  `ava_llm_usage_*` instruments (the OTLP metrics mirror of the `llm_usage`
  event): counts via `increase`, percentiles via `histogram_quantile` over
  the latency histogram. Per-bucket max latency comes from Loki via `unwrap`
  (exact — a Prometheus histogram only bounds the max by its top bucket);
  errors from the LLM error family events in Loki.
- **restarts** — `agent_restarted` + `service_started` counts from Loki,
  plus whole-window breakdowns: services by `attributes.name` (counts from a
  grouped count, `last_start` from one bounded newest-first fetch of the
  rare `service_started` events), agents by the stream `agent_id` label
  (top 20; labels from the `agents` registry — the one SQL read left, the
  agents table is core cluster state, not the retired events storage).

Fidelity notes vs the PG reader:
- Counter deltas (`increase`) are Prometheus extrapolations, not exact
  bucket sums: a counter that started mid-bucket is attributed across the
  whole bucket, and a process restart resets the counter (increase handles
  the reset but smears the window). Counts are rounded to int; token and TPS
  values are close but not exact.
- The in-progress bucket is partial, as before.
- `latency_max_ms` and the totals keep their old exactness (Loki unwrap max /
  merged series sums); p50/p95 are histogram approximations both here and in
  the old reader.

Queries run in parallel on one module-level bounded executor: every group
submits its leaf queries up front and assembles afterwards, so the whole
fan-out runs concurrently without nested per-request pools (the old shape
built up to 21 pool threads per call) and concurrent panel polls share —
not multiply — the concurrency cap against the single-box LGTM stack.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from gateway import loki_events, prom_metrics
from shared.events.contract import LLM_ERROR_FAMILY, OPS_GRID_ORIGIN, family_events

# Window -> (seconds, bucket seconds). Bucket count per window is fixed:
# 1h=60, 6h=72, 24h=48, 7d=168 points — enough shape, small payloads.
WINDOWS: dict[str, tuple[int, int]] = {
    "1h": (3600, 60),
    "6h": (21600, 300),
    "24h": (86400, 1800),
    "7d": (604800, 3600),
}

_GRID_ORIGIN = OPS_GRID_ORIGIN
_LLM_ERROR_EVENTS = family_events(LLM_ERROR_FAMILY)

# OTLP instrument names on the Prometheus side (see shared/telemetry_otlp):
# int payload fields -> counters named `ava_<event>_<field>` (Prometheus
# appends `_total`), float fields -> histograms (`_bucket`/`_sum`/`_count`).
_LLM_CALLS = "ava_llm_usage_latency_milliseconds_count"
_LLM_TOKENS_IN = "ava_llm_usage_in_total"
_LLM_TOKENS_OUT = "ava_llm_usage_out_total"
_LLM_TOKENS_REASONING = "ava_llm_usage_reasoning_total"
_LLM_LATENCY_SUM = "ava_llm_usage_latency_milliseconds_sum"
_LLM_LATENCY_HIST = "ava_llm_usage_latency_milliseconds_bucket"

# One bounded executor for every leaf query, shared across requests — 18
# workers is the previous per-request pools' aggregate capacity (3 sse +
# 10 llm + 5 restarts), so a single call's fan-out is no less parallel than
# before while pool construction/teardown per request is gone.
_POOL = ThreadPoolExecutor(max_workers=18, thread_name_prefix="ops-lgtm")


def _bucket_starts(anchor: datetime, window_s: int, bucket_s: int) -> list[datetime]:
    """Bucket boundary times covering `[anchor - window, anchor]`, oldest
    first, on the fixed 60s grid. The last bucket is the in-progress one
    (start <= anchor < start + bucket_s), so the panel's live-most point is
    always present; the series arrays are indexed by position in this list.
    Same grid the PG reader used."""
    n = window_s // bucket_s
    elapsed = int((anchor - _GRID_ORIGIN).total_seconds())
    last = _GRID_ORIGIN + timedelta(seconds=elapsed - (elapsed % bucket_s))
    return [last - timedelta(seconds=(n - 1 - i) * bucket_s) for i in range(n)]


def _round1(v: float | None) -> float | None:
    return round(v, 1) if v is not None else None


def _bucket_values(
    points: list[tuple[int, float]] | list[tuple[int, int]],
    *,
    bucket_starts: list[datetime],
    bucket_s: int,
) -> list[float | None]:
    """Map sparse `(ts_s, value)` points onto the bucket grid. Points are the
    range queries' evaluation timestamps — bucket END times (`start + i*step`),
    so a point at `t` belongs to the bucket that ends at `t`. `None` = no
    data for that bucket."""
    origin_s = int(_GRID_ORIGIN.timestamp())
    first_idx = (int(bucket_starts[0].timestamp()) - origin_s) // bucket_s
    n = len(bucket_starts)
    out: list[float | None] = [None] * n
    for ts_s, v in points:
        idx = (ts_s - origin_s) // bucket_s - first_idx - 1
        if 0 <= idx < n:
            out[idx] = float(v)
    return out


def _counts(values: list[float | None]) -> list[int]:
    return [0 if v is None else round(v) for v in values]


# ── sse ────────────────────────────────────────────────────────────────────


def _sse_submit(*, from_: datetime, to: datetime, bucket_s: int) -> dict[str, Future[Any]]:
    return {
        "sse": _POOL.submit(
            loki_events.count_events_series,
            event_names=["sse_drop"],
            group_by="kind",
            from_attributes=True,
            from_=from_,
            to=to,
            step_s=bucket_s,
        ),
        "evlog": _POOL.submit(
            loki_events.count_events_series,
            event_names=["event_log_drop"],
            from_=from_,
            to=to,
            step_s=bucket_s,
        ),
    }


def _sse_series(
    futs: dict[str, Future[Any]], *, bucket_starts: list[datetime], bucket_s: int
) -> dict[str, Any]:
    grouped = futs["sse"].result()
    evlog = futs["evlog"].result().get("", [])
    # kind='queue_full' vs everything else; rows without a kind count
    # toward neither (old FILTER semantics).
    queue_full = _counts(
        _bucket_values(
            grouped.get("queue_full", []), bucket_starts=bucket_starts, bucket_s=bucket_s
        )
    )
    publish_error = _counts(
        _bucket_values(
            [
                (ts, float(v))
                for k, pts in grouped.items()
                if k not in ("queue_full", "")
                for ts, v in pts
            ],
            bucket_starts=bucket_starts,
            bucket_s=bucket_s,
        )
    )
    event_log_drop = _counts(_bucket_values(evlog, bucket_starts=bucket_starts, bucket_s=bucket_s))
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


# ── llm ────────────────────────────────────────────────────────────────────


def _llm_submit(*, bucket_starts: list[datetime], bucket_s: int) -> dict[str, Future[Any]]:
    from_ = bucket_starts[0]
    to = bucket_starts[-1] + timedelta(seconds=bucket_s)
    n = len(bucket_starts)
    window_s = int((to - from_).total_seconds())

    def prom_series(expr: str) -> list[float | None]:
        """Per-bucket values of one range query (summed across result series)."""
        rows = prom_metrics.query_range(expr, start=from_, end=to, step_s=bucket_s)
        out: list[float | None] = [None] * n
        for _labels, values in rows:
            filled = _bucket_values(values, bucket_starts=bucket_starts, bucket_s=bucket_s)
            for i, v in enumerate(filled):
                if v is not None:
                    cur = out[i]
                    out[i] = v if cur is None else cur + v
        return out

    def instant(expr: str) -> float | None:
        rows = prom_metrics.query(expr)
        return rows[0][1] if rows else None

    return {
        "calls": _POOL.submit(prom_series, f"sum(increase({_LLM_CALLS}[{bucket_s}s]))"),
        "tin": _POOL.submit(prom_series, f"sum(increase({_LLM_TOKENS_IN}[{bucket_s}s]))"),
        "tout": _POOL.submit(prom_series, f"sum(increase({_LLM_TOKENS_OUT}[{bucket_s}s]))"),
        "treason": _POOL.submit(
            prom_series, f"sum(increase({_LLM_TOKENS_REASONING}[{bucket_s}s]))"
        ),
        "latsum": _POOL.submit(prom_series, f"sum(increase({_LLM_LATENCY_SUM}[{bucket_s}s]))"),
        "p50": _POOL.submit(
            prom_series,
            f"histogram_quantile(0.5, sum by (le) (rate({_LLM_LATENCY_HIST}[{bucket_s}s])))",
        ),
        "p95": _POOL.submit(
            prom_series,
            f"histogram_quantile(0.95, sum by (le) (rate({_LLM_LATENCY_HIST}[{bucket_s}s])))",
        ),
        "max": _POOL.submit(
            loki_events.attribute_max_series,
            field="latency_ms",
            event_names=["llm_usage"],
            from_=from_,
            to=to,
            step_s=bucket_s,
        ),
        "err": _POOL.submit(
            loki_events.count_events_series,
            event_names=list(_LLM_ERROR_EVENTS),
            from_=from_,
            to=to,
            step_s=bucket_s,
        ),
        "total_p50": _POOL.submit(
            instant,
            f"histogram_quantile(0.5, sum by (le) (increase({_LLM_LATENCY_HIST}[{window_s}s])))",
        ),
        "total_p95": _POOL.submit(
            instant,
            f"histogram_quantile(0.95, sum by (le) (increase({_LLM_LATENCY_HIST}[{window_s}s])))",
        ),
    }


def _llm_series(
    futs: dict[str, Future[Any]], *, bucket_starts: list[datetime], bucket_s: int
) -> dict[str, Any]:
    n = len(bucket_starts)
    calls = _counts(futs["calls"].result())
    tokens_in = _counts(futs["tin"].result())
    tokens_out = _counts(futs["tout"].result())
    tokens_reasoning = _counts(futs["treason"].result())
    lat_sum = _counts(futs["latsum"].result())
    p50 = futs["p50"].result()
    p95 = futs["p95"].result()
    lat_max = _bucket_values(futs["max"].result(), bucket_starts=bucket_starts, bucket_s=bucket_s)
    errors = _counts(
        _bucket_values(
            futs["err"].result().get("", []), bucket_starts=bucket_starts, bucket_s=bucket_s
        )
    )
    total_p50 = futs["total_p50"].result()
    total_p95 = futs["total_p95"].result()

    series: list[dict[str, Any]] = []
    for i in range(n):
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
    t_max = [v for v in lat_max if v is not None]
    return {
        "series": series,
        "totals": {
            "calls": sum(calls),
            "latency_p50_ms": _round1(total_p50),
            "latency_p95_ms": _round1(total_p95),
            "latency_max_ms": _round1(max(t_max)) if t_max else None,
            "tokens_in": sum(tokens_in),
            "tokens_out": sum(tokens_out),
            "tps": _round1(t_tokens / (t_lat / 1000.0)) if t_lat else None,
            "errors": sum(errors),
        },
    }


# ── restarts ───────────────────────────────────────────────────────────────


def _restart_submit(*, from_: datetime, to: datetime, bucket_s: int) -> dict[str, Future[Any]]:
    return {
        "agent": _POOL.submit(
            loki_events.count_events_series,
            event_names=["agent_restarted"],
            from_=from_,
            to=to,
            step_s=bucket_s,
        ),
        "service": _POOL.submit(
            loki_events.count_events_series,
            event_names=["service_started"],
            from_=from_,
            to=to,
            step_s=bucket_s,
        ),
        "services_by_name": _POOL.submit(
            loki_events.count_grouped,
            event_names=["service_started"],
            group_by="name",
            from_attributes=True,
            from_=from_,
            to=to,
        ),
        "agents_by_id": _POOL.submit(
            loki_events.count_grouped,
            event_names=["agent_restarted"],
            group_by="agent_id",
            from_=from_,
            to=to,
        ),
        # last_start per service: one bounded newest-first fetch of the rare
        # service_started events (the sliced whole-window projected-line scan
        # this replaces cost a count pre-query + up to 256 slice fetches). A
        # name whose newest start falls off the 1000-row page keeps its
        # grouped-count entry with last_start None.
        "rows": _POOL.submit(
            loki_events.query_events,
            event_names=["service_started"],
            from_=from_,
            to=to,
            limit=1000,
        ),
    }


def _restart_series(
    futs: dict[str, Future[Any]],
    *,
    bucket_starts: list[datetime],
    bucket_s: int,
    label_lookup: Callable[[list[int]], dict[int, str | None]] | None,
) -> dict[str, Any]:
    agent_restarts = _counts(
        _bucket_values(
            futs["agent"].result().get("", []), bucket_starts=bucket_starts, bucket_s=bucket_s
        )
    )
    service_starts = _counts(
        _bucket_values(
            futs["service"].result().get("", []), bucket_starts=bucket_starts, bucket_s=bucket_s
        )
    )
    services_by_name = futs["services_by_name"].result()
    agents_by_id = futs["agents_by_id"].result()
    rows, _ = futs["rows"].result()

    # Whole-window breakdowns. Rows are newest-first, so the first hit per
    # name is its last start.
    last_by_name: dict[str, datetime] = {}
    for row in rows:
        name = str(row["attributes"].get("name") or "").strip()
        if name and name not in last_by_name:
            last_by_name[name] = row["ts"]
    names = sorted(set(services_by_name) | set(last_by_name))
    services: list[dict[str, Any]] = [
        {
            "name": name,
            "starts": services_by_name.get(name, 0),
            "last_start": last_by_name[name].isoformat() if name in last_by_name else None,
        }
        for name in names
    ]
    services.sort(key=lambda s: (-s["starts"], s["name"]))

    top = sorted(
        ((int(aid), c) for aid, c in agents_by_id.items() if aid),
        key=lambda r: (-r[1], r[0]),
    )[:20]
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


# ── entry point ────────────────────────────────────────────────────────────


def fetch_ops_series(
    window: str,
    *,
    label_lookup: Callable[[list[int]], dict[int, str | None]] | None = None,
) -> dict[str, Any]:
    """Run every registered ops series for `window` and return the full report
    dict — meta + the three groups — ready for `OpsMonitorReport(**data)`.

    Reads Loki + Prometheus (the LGTM stack). `label_lookup`, when supplied,
    runs only after every network future has completed, for the agents-registry
    labels in the restart breakdown. This keeps a pooled Postgres connection
    out of bounded-but-slow network waits.

    Every leaf query is submitted to the shared executor before any group is
    assembled, so the whole fan-out runs concurrently — same parallelism as
    the old nested pools, without building them per request.
    """
    window_s, bucket_s = WINDOWS[window]
    anchor = datetime.now(UTC)
    bucket_starts = _bucket_starts(anchor, window_s, bucket_s)
    from_ = bucket_starts[0]
    to = bucket_starts[-1] + timedelta(seconds=bucket_s)

    sse_futs = _sse_submit(from_=from_, to=to, bucket_s=bucket_s)
    llm_futs = _llm_submit(bucket_starts=bucket_starts, bucket_s=bucket_s)
    restart_futs = _restart_submit(from_=from_, to=to, bucket_s=bucket_s)

    return {
        "meta": {
            "window": window,
            "bucket_seconds": bucket_s,
            "generated_at": anchor.isoformat(),
            "bucket_starts": [b.isoformat() for b in bucket_starts],
        },
        "sse": _sse_series(sse_futs, bucket_starts=bucket_starts, bucket_s=bucket_s),
        "llm": _llm_series(llm_futs, bucket_starts=bucket_starts, bucket_s=bucket_s),
        "restarts": _restart_series(
            restart_futs,
            bucket_starts=bucket_starts,
            bucket_s=bucket_s,
            label_lookup=label_lookup,
        ),
    }
