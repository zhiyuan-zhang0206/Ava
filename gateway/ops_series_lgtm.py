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
  grouped count, `last_start` from the projected lines), agents by the
  stream `agent_id` label (top 20; labels from the `agents` registry — the
  one SQL read left, the agents table is core cluster state, not the retired
  events storage).

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

Queries run in parallel (one `ThreadPoolExecutor` per group); the panel polls
every 60s and each query is a small range/instant call.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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


def _sse_series(*, bucket_starts: list[datetime], bucket_s: int) -> dict[str, Any]:
    from_ = bucket_starts[0]
    to = bucket_starts[-1] + timedelta(seconds=bucket_s)

    def run() -> tuple[list[int], list[int], list[int]]:
        with ThreadPoolExecutor(max_workers=3) as pool:
            sse_f = pool.submit(
                loki_events.count_events_series,
                event_names=["sse_drop"],
                group_by="kind",
                from_attributes=True,
                from_=from_,
                to=to,
                step_s=bucket_s,
            )
            evlog_f = pool.submit(
                loki_events.count_events_series,
                event_names=["event_log_drop"],
                from_=from_,
                to=to,
                step_s=bucket_s,
            )
            grouped = sse_f.result()
            evlog = evlog_f.result().get("", [])
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
        event_log_drop = _counts(
            _bucket_values(evlog, bucket_starts=bucket_starts, bucket_s=bucket_s)
        )
        return queue_full, publish_error, event_log_drop

    queue_full, publish_error, event_log_drop = run()
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


def _llm_series(*, bucket_starts: list[datetime], bucket_s: int) -> dict[str, Any]:
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

    with ThreadPoolExecutor(max_workers=10) as pool:
        calls_f = pool.submit(prom_series, f"sum(increase({_LLM_CALLS}[{bucket_s}s]))")
        tin_f = pool.submit(prom_series, f"sum(increase({_LLM_TOKENS_IN}[{bucket_s}s]))")
        tout_f = pool.submit(prom_series, f"sum(increase({_LLM_TOKENS_OUT}[{bucket_s}s]))")
        treason_f = pool.submit(prom_series, f"sum(increase({_LLM_TOKENS_REASONING}[{bucket_s}s]))")
        latsum_f = pool.submit(prom_series, f"sum(increase({_LLM_LATENCY_SUM}[{bucket_s}s]))")
        p50_f = pool.submit(
            prom_series,
            f"histogram_quantile(0.5, sum by (le) (rate({_LLM_LATENCY_HIST}[{bucket_s}s])))",
        )
        p95_f = pool.submit(
            prom_series,
            f"histogram_quantile(0.95, sum by (le) (rate({_LLM_LATENCY_HIST}[{bucket_s}s])))",
        )
        max_f = pool.submit(
            loki_events.attribute_max_series,
            field="latency_ms",
            event_names=["llm_usage"],
            from_=from_,
            to=to,
            step_s=bucket_s,
        )
        err_f = pool.submit(
            loki_events.count_events_series,
            event_names=list(_LLM_ERROR_EVENTS),
            from_=from_,
            to=to,
            step_s=bucket_s,
        )
        total_p50_f = pool.submit(
            instant,
            f"histogram_quantile(0.5, sum by (le) (increase({_LLM_LATENCY_HIST}[{window_s}s])))",
        )
        total_p95_f = pool.submit(
            instant,
            f"histogram_quantile(0.95, sum by (le) (increase({_LLM_LATENCY_HIST}[{window_s}s])))",
        )

        calls = _counts(calls_f.result())
        tokens_in = _counts(tin_f.result())
        tokens_out = _counts(tout_f.result())
        tokens_reasoning = _counts(treason_f.result())
        lat_sum = _counts(latsum_f.result())
        p50 = p50_f.result()
        p95 = p95_f.result()
        lat_max = _bucket_values(max_f.result(), bucket_starts=bucket_starts, bucket_s=bucket_s)
        errors = _counts(
            _bucket_values(
                err_f.result().get("", []), bucket_starts=bucket_starts, bucket_s=bucket_s
            )
        )
        total_p50 = total_p50_f.result()
        total_p95 = total_p95_f.result()

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


def _restart_series(cur: Any, *, bucket_starts: list[datetime], bucket_s: int) -> dict[str, Any]:
    from_ = bucket_starts[0]
    to = bucket_starts[-1] + timedelta(seconds=bucket_s)

    def run() -> tuple[
        list[int],
        list[int],
        dict[str, int],
        dict[str, int],
        list[tuple[int, int | None, str]],
    ]:
        with ThreadPoolExecutor(max_workers=5) as pool:
            agent_f = pool.submit(
                loki_events.count_events_series,
                event_names=["agent_restarted"],
                from_=from_,
                to=to,
                step_s=bucket_s,
            )
            service_f = pool.submit(
                loki_events.count_events_series,
                event_names=["service_started"],
                from_=from_,
                to=to,
                step_s=bucket_s,
            )
            services_by_name_f = pool.submit(
                loki_events.count_grouped,
                event_names=["service_started"],
                group_by="name",
                from_attributes=True,
                from_=from_,
                to=to,
            )
            agents_by_id_f = pool.submit(
                loki_events.count_grouped,
                event_names=["agent_restarted"],
                group_by="agent_id",
                from_=from_,
                to=to,
            )
            lines_f = pool.submit(
                loki_events.query_projected_lines,
                fields=["name"],
                template="{{ .name }}",
                event_names=["service_started"],
                from_=from_,
                to=to,
            )
            agent_restarts = _counts(
                _bucket_values(
                    agent_f.result().get("", []), bucket_starts=bucket_starts, bucket_s=bucket_s
                )
            )
            service_starts = _counts(
                _bucket_values(
                    service_f.result().get("", []), bucket_starts=bucket_starts, bucket_s=bucket_s
                )
            )
            services_by_name = services_by_name_f.result()
            agents_by_id = agents_by_id_f.result()
            lines = lines_f.result()
        return agent_restarts, service_starts, services_by_name, agents_by_id, lines

    agent_restarts, service_starts, services_by_name, agents_by_id, lines = run()

    # Whole-window breakdowns.
    last_by_name: dict[str, int] = {}
    for ts_ns, _aid, line in lines:
        name = line.strip()
        if name:
            last_by_name[name] = max(last_by_name.get(name, 0), ts_ns)
    names = sorted(set(services_by_name) | set(last_by_name))
    services: list[dict[str, Any]] = [
        {
            "name": name,
            "starts": services_by_name.get(name, 0),
            "last_start": datetime.fromtimestamp(last_by_name[name] / 1e9, tz=UTC).isoformat()
            if name in last_by_name
            else None,
        }
        for name in names
    ]
    services.sort(key=lambda s: (-s["starts"], s["name"]))

    top = sorted(
        ((int(aid), c) for aid, c in agents_by_id.items() if aid),
        key=lambda r: (-r[1], r[0]),
    )[:20]
    labels: dict[int, str | None] = {}
    if top and cur is not None:
        ids = [aid for aid, _ in top]
        cur.execute("SELECT id, label FROM agents WHERE id = ANY(%s)", (ids,))
        labels = dict(cur.fetchall())
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


def fetch_ops_series(cur: Any, window: str) -> dict[str, Any]:
    """Run every registered ops series for `window` and return the full report
    dict — meta + the three groups — ready for `OpsMonitorReport(**data)`.

    Reads Loki + Prometheus (the LGTM stack); `cur` is used only for the
    agents-registry label lookup in the restarts breakdown (core cluster
    state, not the retired events storage) and may be None to skip it.
    """
    window_s, bucket_s = WINDOWS[window]
    anchor = datetime.now(UTC)
    bucket_starts = _bucket_starts(anchor, window_s, bucket_s)

    with ThreadPoolExecutor(max_workers=3) as pool:
        sse_f = pool.submit(_sse_series, bucket_starts=bucket_starts, bucket_s=bucket_s)
        llm_f = pool.submit(_llm_series, bucket_starts=bucket_starts, bucket_s=bucket_s)
        restarts_f = pool.submit(
            _restart_series, cur, bucket_starts=bucket_starts, bucket_s=bucket_s
        )
        sse = sse_f.result()
        llm = llm_f.result()
        restarts = restarts_f.result()

    return {
        "meta": {
            "window": window,
            "bucket_seconds": bucket_s,
            "generated_at": anchor.isoformat(),
            "bucket_starts": [b.isoformat() for b in bucket_starts],
        },
        "sse": sse,
        "llm": llm,
        "restarts": restarts,
    }
