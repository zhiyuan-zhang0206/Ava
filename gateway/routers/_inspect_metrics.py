"""SQL-only Inspector statistics over persisted observations and historical days.

New observations own dates at/after the collection's UTC cutover day. Earlier
full days prefer the existing family ledger; raw observations fill missing days
and partial-day edges. The two sources never add the same family's day twice.
Exact quantiles retain their observations; their DB work scales with the selected
turns. Source loss remains explicit and is never repaired by a synchronous log scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from gateway.schemas import AgentActivity, AgentCost, AgentStats, AgentTps, StatsWindowHours
from gateway.schemas.inspect_metrics import InspectMetricsMetadata, MetricEvidence
from gateway.schemas.stats import window_delta

_SUM_FIELDS = (
    "usage_calls",
    "unpriced_calls",
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "tokens_reasoning",
    "cost_usd",
    "turn_total",
    "turn_ok",
    "turn_duration_sum",
    "exec_ok",
    "exec_failed",
    "active_seconds",
    "exec_seconds",
)
_COST_FIELDS = _SUM_FIELDS[:7]
_TURN_FIELDS = _SUM_FIELDS[7:12]


@dataclass(frozen=True)
class MetricsSnapshot:
    """One persisted statistics result; unavailable sections have no invented value."""

    cost: AgentCost | None
    stats: AgentStats | None
    tps: AgentTps | None
    activity: AgentActivity | None
    metadata: InspectMetricsMetadata


def _midnight(value: datetime) -> datetime:
    return datetime.combine(value.astimezone(UTC).date(), time.min, tzinfo=UTC)


def _full_day(day: date, start: datetime, end: datetime) -> bool:
    midnight = datetime.combine(day, time.min, tzinfo=UTC)
    return start <= midnight and midnight + timedelta(days=1) <= end


def _weighted_quantile(q: float, values: list[tuple[float, int]]) -> float:
    """Linear-interpolated sample percentile without expanding repeated values."""
    ordered = sorted(values)
    total = sum(count for _, count in ordered)
    if not total:
        return 0.0
    rank = q * (total - 1)
    lower = int(rank)
    upper = min(lower + 1, total - 1)
    count_so_far = 0
    low_value = high_value = 0.0
    for value, count in ordered:
        if count_so_far <= lower < count_so_far + count:
            low_value = value
        if count_so_far <= upper < count_so_far + count:
            high_value = value
            break
        count_so_far += count
    return low_value + (high_value - low_value) * (rank - lower)


def _rows(conn: Connection[Any], query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)  # pyright: ignore[reportArgumentType] -- fixed internal SQL
        return list(cur.fetchall())


def _observed_days(
    conn: Connection[Any], agent_id: int, start: datetime, end: datetime
) -> dict[date, dict[str, Any]]:
    """Read full-day summaries and reduce only the at-most-two boundary days."""
    full_from = _midnight(start)
    if full_from < start:
        full_from += timedelta(days=1)
    full_to = _midnight(end)
    days = _rows(
        conn,
        "SELECT * FROM agent_metric_days WHERE agent_id=%s AND day>=%s AND day<%s",
        (agent_id, full_from.date(), full_to.date()),
    )
    edges = [(start, min(full_from, end)), (max(full_to, start), end)]
    merged_edges: list[tuple[datetime, datetime]] = []
    for lo, hi in sorted(set(edges)):
        if lo >= hi or (merged_edges and lo < merged_edges[-1][1]):
            continue
        merged_edges.append((lo, hi))
    # Fixed column vocabulary, never request-derived SQL identifiers.
    sums = ", ".join(
        f"COALESCE(sum({('turn_duration_seconds' if name == 'turn_duration_sum' else name)}),0) AS {name}"
        for name in _SUM_FIELDS
    )
    for lo, hi in merged_edges:
        days.extend(
            _rows(
                conn,
                "SELECT (occurred_at AT TIME ZONE 'UTC')::date AS day, "  # noqa: S608 -- fixed metric vocabulary
                + sums
                + ", min(turn_duration_seconds) AS turn_duration_min, "
                "max(turn_duration_seconds) AS turn_duration_max, "
                "max(observed_at) AS last_observed_at FROM agent_metric_observations "
                "WHERE agent_id=%s AND occurred_at>=%s AND occurred_at<%s GROUP BY 1",
                (agent_id, lo, hi),
            )
        )
    return {row["day"]: row for row in days}


def _legacy_days(
    conn: Connection[Any],
    agent_id: int,
    start: datetime,
    end: datetime,
    cutover: date,
) -> tuple[dict[date, dict[str, Any]], dict[date, dict[str, Any]]]:
    """Read historical evidence without allowing the old writer to own new dates."""
    params = (agent_id, start.date(), min(end.date(), cutover))
    costs = _rows(
        conn,
        "SELECT day, sum(llm_calls) AS usage_calls, sum(unpriced_calls) AS unpriced_calls, "
        "sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out, "
        "sum(tokens_cached) AS tokens_cached, sum(tokens_reasoning) AS tokens_reasoning, "
        "sum(cost_usd) AS cost_usd FROM agent_model_tokens_daily "
        "WHERE agent_id=%s AND day>=%s AND day<%s GROUP BY day",
        params,
    )
    turns = _rows(
        conn,
        "SELECT day, turn_total, turn_ok, turn_dur_sum AS turn_duration_sum, "
        "turn_dur_min AS turn_duration_min, turn_dur_max AS turn_duration_max, "
        "turn_dur_hist, exec_ok, exec_failed FROM agent_metrics_daily "
        "WHERE agent_id=%s AND day>=%s AND day<%s",
        params,
    )
    return (
        {row["day"]: row for row in costs if _full_day(row["day"], start, end)},
        {row["day"]: row for row in turns if _full_day(row["day"], start, end)},
    )


def _evidence(
    *,
    historical: bool,
    present: bool,
    sources: list[str],
    precision: str | None = None,
) -> MetricEvidence:
    availability = "partial" if historical else "observed"
    if historical and not present:
        availability = "unavailable"
    return MetricEvidence(
        availability=availability,  # pyright: ignore[reportArgumentType]
        sources=sources,
        reason="Historical collection coverage is unknown." if historical else None,
        duration_precision=precision,  # pyright: ignore[reportArgumentType]
    )


def _alive_seconds(
    conn: Connection[Any],
    agent_id: int,
    start: datetime,
    end: datetime,
) -> tuple[float, bool]:
    rows = _rows(
        conn,
        "SELECT COALESCE(sum(EXTRACT(epoch FROM "
        "least(COALESCE(ended_at,%s),%s)-greatest(started_at,%s))),0) AS seconds, "
        "count(*) AS intervals FROM agent_lifecycle_intervals "
        "WHERE agent_id=%s AND started_at<%s AND COALESCE(ended_at,%s)>%s",
        (end, end, start, agent_id, end, end, start),
    )
    return float(rows[0]["seconds"]), bool(rows[0]["intervals"])


def inspect_snapshot(
    pool: ConnectionPool[Any],
    agent_id: int,
    hours: StatsWindowHours | None,
    *,
    since_compact: bool,
    spawned_at: datetime,
    last_compact_at: datetime | None = None,
) -> MetricsSnapshot:
    """Read one consistent DB snapshot; no HTTP/Loki request belongs on this path.

    A missing durable compact boundary makes that window unavailable. Historical
    evidence remains partial even after a successful backfill, because the old
    best-effort event producer cannot certify that every observation survived.
    """
    sampled_at = datetime.now(UTC)
    start = spawned_at if hours is None else sampled_at - window_delta(hours)
    if since_compact and last_compact_at is not None:
        start = last_compact_at
    with pool.connection(timeout=1.0) as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '2s'")
        conn.execute("SET LOCAL lock_timeout = '200ms'")
        collection = _rows(conn, "SELECT started_at FROM agent_metric_collection", ())[0][
            "started_at"
        ]
        if since_compact and last_compact_at is None:
            absent = MetricEvidence(
                availability="unavailable",
                sources=[],
                reason="No authoritative completed compaction boundary is available.",
            )
            return MetricsSnapshot(
                None,
                None,
                None,
                None,
                InspectMetricsMetadata(
                    window_start=None,
                    window_end=sampled_at,
                    sampled_at=sampled_at,
                    collection_started_at=collection,
                    last_observed_at=None,
                    cost=absent,
                    turns=absent,
                    activity=absent,
                    lifecycle=absent,
                ),
            )
        return _read_snapshot(conn, agent_id, start, sampled_at, spawned_at, collection)


def _sum_days(
    observed: dict[date, dict[str, Any]],
    costs: dict[date, dict[str, Any]],
    turns: dict[date, dict[str, Any]],
) -> tuple[dict[str, Any], list[float]]:
    """Choose one cost and turn source per day before additive reduction."""
    totals: dict[str, Any] = dict.fromkeys(_SUM_FIELDS, 0)
    totals["cost_usd"] = Decimal(0)
    extrema: list[float] = []
    for day in observed.keys() | costs.keys() | turns.keys():
        raw = observed.get(day, {})
        for keys, selected in (
            (_COST_FIELDS, costs.get(day, raw)),
            (_TURN_FIELDS, turns.get(day, raw)),
        ):
            for key in keys:
                value = selected.get(key, 0)
                totals[key] += (
                    Decimal(str(value))
                    if key == "cost_usd"
                    else float(value)
                    if key == "turn_duration_sum"
                    else int(value)
                )
        for key in ("active_seconds", "exec_seconds"):
            totals[key] += float(raw.get(key, 0))
        for key in ("turn_duration_min", "turn_duration_max"):
            value = turns.get(day, raw).get(key)
            if value is not None:
                extrema.append(float(value))
    return totals, extrema


def _duration_distribution(
    conn: Connection[Any],
    agent_id: int,
    start: datetime,
    end: datetime,
    observed: dict[date, dict[str, Any]],
    turns: dict[date, dict[str, Any]],
) -> tuple[list[tuple[float, int]], str]:
    # Group exact samples once. An old daily histogram remains authoritative
    # until all of its turn duration observations survive replay; equal event
    # counts alone do not prove that optional durations were recorded.
    exact_rows = _rows(
        conn,
        "SELECT (occurred_at AT TIME ZONE 'UTC')::date AS day, "
        "turn_duration_seconds AS duration, count(*) AS n "
        "FROM agent_metric_observations WHERE agent_id=%s AND occurred_at>=%s "
        "AND occurred_at<%s AND turn_duration_seconds IS NOT NULL "
        "GROUP BY 1,2",
        (agent_id, start, end),
    )
    duration_counts: dict[date, int] = {}
    for row in exact_rows:
        duration_counts[row["day"]] = duration_counts.get(row["day"], 0) + int(row["n"])
    bucket_days = {
        day: row
        for day, row in turns.items()
        if day not in observed
        or observed[day]["turn_total"] != row["turn_total"]
        or duration_counts.get(day, 0) != row["turn_total"]
    }
    exact = [row for row in exact_rows if row["day"] not in bucket_days]
    distribution = [(float(row["duration"]), int(row["n"])) for row in exact]
    bucketed = False
    for row in bucket_days.values():
        histogram = row["turn_dur_hist"]
        bucketed |= bool(histogram)
        distribution.extend((float(value), int(count)) for value, count in histogram.items())
    precision = "mixed" if bucketed and exact else "one_second_buckets" if bucketed else "exact"
    return distribution, precision


def _cost_and_turns(
    totals: dict[str, Any],
    extrema: list[float],
    distribution: list[tuple[float, int]],
    cost_evidence: MetricEvidence,
    turn_evidence: MetricEvidence,
) -> tuple[AgentCost | None, AgentStats | None]:
    """Project numeric evidence; absent families and durations remain null."""
    cost = (
        None
        if cost_evidence.availability == "unavailable"
        else AgentCost(
            cost_usd=float(round(totals["cost_usd"], 4)),
            unpriced_calls=int(totals["unpriced_calls"]),
            llm_calls=int(totals["usage_calls"]),
            tokens_in=int(totals["tokens_in"]),
            tokens_out=int(totals["tokens_out"]),
            tokens_cached=int(totals["tokens_cached"]),
            tokens_reasoning=int(totals["tokens_reasoning"]),
            cache_hit_pct=min(100, round(totals["tokens_cached"] / totals["tokens_in"] * 100, 2))
            if totals["tokens_in"]
            else 0,
        )
    )
    stats = (
        None
        if turn_evidence.availability == "unavailable"
        else AgentStats(
            turn_total=int(totals["turn_total"]),
            turn_ok=int(totals["turn_ok"]),
            turn_p50_seconds=round(_weighted_quantile(0.5, distribution), 2)
            if distribution or not totals["turn_total"]
            else None,
            turn_p90_seconds=round(_weighted_quantile(0.9, distribution), 2)
            if distribution or not totals["turn_total"]
            else None,
            turn_min_seconds=round(min(extrema, default=0), 2)
            if extrema or not totals["turn_total"]
            else None,
            turn_max_seconds=round(max(extrema, default=0), 2)
            if extrema or not totals["turn_total"]
            else None,
            exec_ok=int(totals["exec_ok"]),
            exec_failed=int(totals["exec_failed"]),
        )
    )
    return cost, stats


def _throughput_and_activity(
    totals: dict[str, Any],
    cost: AgentCost | None,
    stats: AgentStats | None,
    *,
    durations_known: bool,
    historical: bool,
    alive: float,
    whole_alive: float,
    metadata: InspectMetricsMetadata,
) -> tuple[AgentTps | None, AgentActivity | None]:
    # Whole-lifetime coverage is independent from a recent selected window.
    # Preserve LM-stage throughput even when the lifetime denominator is unknown.
    tps = None
    activity = None
    if cost is not None and stats is not None:
        tps = AgentTps(
            lm_stage_tps=(
                round(totals["tokens_out"] / totals["turn_duration_sum"], 2)
                if totals["turn_duration_sum"]
                else 0
            )
            if durations_known
            else None,
            agent_lifecycle_tps=(round(totals["tokens_out"] / whole_alive, 2) if whole_alive else 0)
            if metadata.lifecycle.availability == "observed"
            else None,
        )
    if not historical and metadata.activity.availability != "unavailable":
        activity = AgentActivity(
            active_seconds=round(totals["active_seconds"], 2),
            alive_seconds=round(alive, 2),
            active_rate=min(1, round(totals["active_seconds"] / alive, 4)) if alive else 0,
            llm_seconds=round(totals["turn_duration_sum"], 2) if durations_known else None,
            exec_seconds=round(totals["exec_seconds"], 2),
        )
    return tps, activity


def _read_snapshot(
    conn: Connection[Any],
    agent_id: int,
    start: datetime,
    end: datetime,
    spawned_at: datetime,
    collection: datetime,
) -> MetricsSnapshot:
    observed = _observed_days(conn, agent_id, start, end)
    # Birth cannot have observations before itself; its first partial UTC day
    # is therefore a complete lifetime ledger day without estimating an edge.
    legacy_start = _midnight(start) if start == spawned_at else start
    costs, turns = _legacy_days(
        conn, agent_id, legacy_start, end, collection.astimezone(UTC).date()
    )
    totals, extrema = _sum_days(observed, costs, turns)
    distribution, precision = _duration_distribution(conn, agent_id, start, end, observed, turns)
    durations_known = sum(count for _, count in distribution) == totals["turn_total"]
    historical = max(start, spawned_at) < collection
    cost_evidence = _evidence(
        historical=historical,
        present=bool(costs) or any(row["usage_calls"] for row in observed.values()),
        sources=["observations", *(["historical_daily_costs"] if costs else [])],
    )
    turn_evidence = _evidence(
        historical=historical,
        present=bool(turns)
        or any(
            row["turn_total"] or row["exec_ok"] or row["exec_failed"] for row in observed.values()
        ),
        sources=["observations", *(["historical_daily_turns"] if turns else [])],
        precision=precision,
    )
    if not durations_known:
        turn_evidence = turn_evidence.model_copy(
            update={
                "availability": "partial",
                "reason": "Some observed turns have no retained duration observation.",
            }
        )
    if historical and (precision != "exact" or turn_evidence.availability == "unavailable"):
        retained = _rows(
            conn,
            "SELECT EXISTS (SELECT 1 FROM agent_archive_stats WHERE agent_id=%s "
            "AND turn_distribution <> '[]'::jsonb) AS present",
            (agent_id,),
        )[0]["present"]
        if retained:
            turn_evidence = turn_evidence.model_copy(
                update={
                    "retained_unapplied_sources": ["historical_archive_distribution"],
                    "reason": "Exact frozen-archive durations remain retained without event timestamps; they cannot be attributed to this window.",
                }
            )
    activity_evidence = _evidence(
        historical=historical, present=bool(observed), sources=["observations"]
    )
    alive, intervals = _alive_seconds(conn, agent_id, start, end)
    whole_alive, _ = _alive_seconds(conn, agent_id, spawned_at, end)
    lifecycle_evidence = _evidence(
        historical=spawned_at < collection, present=intervals, sources=["state_transitions"]
    )
    metadata = InspectMetricsMetadata(
        window_start=start,
        window_end=end,
        sampled_at=end,
        collection_started_at=collection,
        last_observed_at=max((r["last_observed_at"] for r in observed.values()), default=None),
        cost=cost_evidence,
        turns=turn_evidence,
        activity=activity_evidence,
        lifecycle=lifecycle_evidence,
    )
    cost, stats = _cost_and_turns(totals, extrema, distribution, cost_evidence, turn_evidence)
    tps, activity = _throughput_and_activity(
        totals,
        cost,
        stats,
        durations_known=durations_known,
        historical=historical,
        alive=alive,
        whole_alive=whole_alive,
        metadata=metadata,
    )
    return MetricsSnapshot(cost, stats, tps, activity, metadata)
