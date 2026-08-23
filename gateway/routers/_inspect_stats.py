"""Window planning and consolidated bounded-Loki turn stats for inspect.

When the newest metrics-ledger day remains inside Loki retention, inspect
excludes that potentially stale day from the ledger and rereads it entirely
from live events. Late writes into a closed day are therefore neither lost nor
double counted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from math import floor
from typing import Any, NamedTuple

from psycopg_pool import ConnectionPool

from gateway import loki_events
from gateway.routers import _agent_cost, _inspect_pg
from gateway.schemas import AgentStats


class DayPlan(NamedTuple):
    """Full ledger days and the at-most-two raw-event edges around them."""

    day_from: date | None
    day_to: date | None
    edges: list[tuple[datetime, datetime]]
    has_full_days: bool


class StatsValues(NamedTuple):
    """Response stats plus the non-wire duration sum shared by two sections."""

    stats: AgentStats
    turn_duration_seconds: float


class InspectValues(NamedTuple):
    """All inspector values derived from one ledger/archive/live snapshot."""

    stats: AgentStats
    turn_duration_seconds: float
    output_tokens: int
    active_seconds: float
    exec_seconds: float
    lifecycle_events: list[tuple[datetime, str]]


class _LiveInspectValues(NamedTuple):
    """One projected event stream reduced for every inspect history section."""

    turn_total: int
    turn_ok: int
    exec_ok: int
    exec_failed: int
    turn_durations: list[float]
    distribution: list[tuple[float, int]]
    output_tokens: float
    active_seconds: float
    exec_seconds: float
    lifecycle_events: list[tuple[datetime, str]]


def _utc_midnight(value: datetime) -> datetime:
    return datetime.combine(value.date(), time.min, tzinfo=UTC)


def full_day_plan(from_: datetime | None, to: datetime | None) -> DayPlan:
    """Partition a window into ledger-safe full UTC days and raw-event edges."""
    end = to or datetime.now(tz=UTC)
    end_midnight = _utc_midnight(end)
    day_to = end.date() - timedelta(days=1)
    trailing = [] if end == end_midnight else [(end_midnight, end)]
    if from_ is None:
        return DayPlan(None, day_to, trailing, has_full_days=True)

    start_midnight = _utc_midnight(from_)
    day_from = from_.date() if from_ == start_midnight else from_.date() + timedelta(days=1)
    leading = [] if from_ == start_midnight else [(from_, start_midnight + timedelta(days=1))]
    if day_from > day_to:
        return DayPlan(None, None, [(from_, end)], has_full_days=False)
    return DayPlan(day_from, day_to, [*leading, *trailing], has_full_days=True)


def _merge_spans(spans: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Coalesce overlapping raw spans before querying their shared envelope."""
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def live_edge_spans(
    plan: DayPlan,
    watermark: datetime | None,
    from_: datetime | None,
    to: datetime | None,
) -> list[tuple[datetime, datetime]]:
    """Return retained raw edges plus the ledger's unrolled live tail.

    A lagging daily rollup leaves completed days after ``watermark``; include
    them through the requested end rather than silently treating them as read.
    """
    floor_at = _agent_cost._retention_floor()
    now = datetime.now(tz=UTC)
    end = min(to or now, now)
    tail_start = max(watermark or floor_at, from_ or floor_at)
    spans = [*plan.edges]
    if tail_start < end:
        spans.append((tail_start, end))
    return [
        (max(start, floor_at), min(end, now))
        for start, end in _merge_spans(spans)
        if max(start, floor_at) < min(end, now)
    ]


def merge_distribution(distributions: list[list[tuple[float, int]]]) -> list[tuple[float, int]]:
    """Merge archive and live turn durations by integer-second buckets.

    The archive/live seam combines different retention sources, so shared
    integer-second buckets keep its percentile histogram coherent.
    """
    merged: dict[int, int] = {}
    for distribution in distributions:
        for value, count in distribution:
            bucket = floor(value)
            merged[bucket] = merged.get(bucket, 0) + count
    return [(float(value), count) for value, count in sorted(merged.items())]


def merge_exact_distribution(
    distributions: list[list[tuple[float, int]]],
) -> list[tuple[float, int]]:
    """Merge one-source duration values without reducing their precision."""
    merged: dict[float, int] = {}
    for distribution in distributions:
        for value, count in distribution:
            merged[value] = merged.get(value, 0) + count
    return sorted(merged.items())


def _in_spans(value: datetime, spans: list[tuple[datetime, datetime]]) -> bool:
    """Whether a Loki timestamp belongs to one of the existing live windows."""
    return any(start <= value <= end for start, end in spans)


def _attribute_text(attributes: dict[str, Any], key: str) -> str:
    """Match JSON/JSONB text filters, including JSON booleans and missing keys."""
    value = attributes.get(key)
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _attribute_number(attributes: dict[str, Any], key: str) -> float | None:
    """Return a numeric payload value when Loki's former unwrap would retain it."""
    try:
        return float(attributes[key])
    except (KeyError, TypeError, ValueError):
        return None


def _projected_live_values(
    agent_id: int,
    *,
    stats_spans: list[tuple[datetime, datetime]],
    token_spans: list[tuple[datetime, datetime]],
    distribution_window: tuple[datetime, datetime] | None,
    activity_window: tuple[datetime, datetime] | None,
    lifecycle_window: tuple[datetime, datetime] | None,
) -> _LiveInspectValues:
    """Reduce one raw Loki line pass over every live interval inspect needs.

    The pass uses one envelope rather than one aggregation shape per interval.
    A row is still admitted to each ledger/archive seam only through that
    section's original span, preserving the existing double-count prevention.
    """
    spans = [*stats_spans, *token_spans]
    for window in (distribution_window, activity_window, lifecycle_window):
        if window is not None:
            spans.append(window)
    if not spans:
        return _LiveInspectValues(0, 0, 0, 0, [], [], 0.0, 0.0, 0.0, [])

    rows = loki_events.query_projected_lines(
        fields=[],
        template="{{ __line__ }}",
        agent_id=agent_id,
        event_names=[
            "^turn_end$",
            "^exec$",
            "^exec_.*",
            "^exec\\(.*",
            "^llm_usage$",
            "^node_exit$",
            "^agent_spawned$",
            "^agent_resurrected$",
            "^agent_terminated$",
        ],
        from_=min(start for start, _ in spans),
        to=max(end for _, end in spans),
    )

    turn_total = turn_ok = exec_ok = exec_failed = 0
    turn_durations: list[float] = []
    distribution: list[tuple[float, int]] = []
    output_tokens = active_seconds = exec_seconds = 0.0
    lifecycle_events: list[tuple[datetime, str]] = []
    seen: set[tuple[int, str]] = set()

    for ts_ns, _row_agent_id, line in rows:
        key = (ts_ns, line)
        if key in seen:
            continue
        seen.add(key)
        row = loki_events._parse_line(line, ts_ns)
        if row is None:
            continue
        timestamp = datetime.fromtimestamp(ts_ns / 1e9, UTC)
        event_name = row["event_name"]
        category = row["category"]
        attributes = row["attributes"]
        telemetry_or_log = category in {"telemetry", "log"}
        duration = _attribute_number(attributes, "duration_seconds")

        if telemetry_or_log and _in_spans(timestamp, stats_spans):
            if event_name == "turn_end":
                turn_total += 1
                if _attribute_text(attributes, "ok") == "true":
                    turn_ok += 1
                if duration is not None:
                    turn_durations.append(duration)
            elif event_name == "exec":
                exec_ok += 1
            elif event_name.startswith(("exec_", "exec(")):
                exec_failed += 1

        if (
            telemetry_or_log
            and event_name == "turn_end"
            and duration is not None
            and distribution_window is not None
            and _in_spans(timestamp, [distribution_window])
        ):
            distribution.append((duration, 1))

        if telemetry_or_log and event_name == "llm_usage" and _in_spans(timestamp, token_spans):
            output_tokens += _attribute_number(attributes, "out_total") or 0.0

        if (
            event_name == "node_exit"
            and duration is not None
            and activity_window is not None
            and _in_spans(timestamp, [activity_window])
        ):
            node = _attribute_text(attributes, "node")
            if node != "claim":
                active_seconds += duration
            if node == "exec":
                exec_seconds += duration

        if (
            event_name in {"agent_spawned", "agent_resurrected", "agent_terminated"}
            and lifecycle_window is not None
            and _in_spans(timestamp, [lifecycle_window])
        ):
            lifecycle_events.append((row["ts"], event_name))

    return _LiveInspectValues(
        turn_total,
        turn_ok,
        exec_ok,
        exec_failed,
        turn_durations,
        distribution,
        output_tokens,
        active_seconds,
        exec_seconds,
        lifecycle_events,
    )


def _turn_distribution(
    archive: list[tuple[float, int]], live: list[tuple[float, int]]
) -> list[tuple[float, int]]:
    """Merge the frozen archive and live values without reducing precision."""
    return merge_exact_distribution([archive, live])


def inspect_values(
    pool: ConnectionPool[Any], agent_id: int, from_: datetime | None, to: datetime | None
) -> InspectValues:
    """Build stats, TPS, and activity inputs from one archive/ledger/live view."""
    plan = full_day_plan(from_, to)
    with pool.connection() as conn, conn.cursor() as cur:
        freeze = _inspect_pg.freeze_point(cur)
        if plan.has_full_days:
            max_day = _inspect_pg.newest_ledger_day(
                conn, agent_id=agent_id, day_from=plan.day_from, day_to=plan.day_to
            )
            floor_at = _agent_cost._retention_floor()
            gap_live = (
                max_day is not None and datetime.combine(max_day, time.min, tzinfo=UTC) >= floor_at
            )
            ledger_day_to = (
                plan.day_to if max_day is None or not gap_live else max_day - timedelta(days=1)
            )
            ledger = _inspect_pg.ledger_stats(
                conn, agent_id=agent_id, day_from=plan.day_from, day_to=ledger_day_to
            )
            ledger_tokens, token_watermark = _inspect_pg.ledger_tokens(
                conn, agent_id=agent_id, day_from=plan.day_from, day_to=plan.day_to
            )
        else:
            ledger = (0, 0, 0.0, None, None, 0, 0, None)
            ledger_tokens = 0
            token_watermark = None
        archive_distribution = _inspect_pg.archive_distribution(
            conn,
            field="duration_seconds",
            agent_id=agent_id,
            event_names=["^turn_end$"],
            categories=["telemetry", "log"],
            attribute_filters=None,
            from_=from_,
            to=to,
        )
        archive_active_seconds = _inspect_pg.archive_aggregate(
            conn,
            field="duration_seconds",
            agg="sum",
            event_names=["^node_exit$"],
            categories=None,
            agent_id=agent_id,
            from_=from_,
            to=to,
            attribute_filters={"node": "!=claim"},
        )
        archive_exec_seconds = _inspect_pg.archive_aggregate(
            conn,
            field="duration_seconds",
            agg="sum",
            event_names=["^node_exit$"],
            categories=None,
            agent_id=agent_id,
            from_=from_,
            to=to,
            attribute_filters={"node": "exec"},
        )
        archive_lifecycle = _inspect_pg.archive_lifecycle(
            conn, agent_id=agent_id, from_=None, to=None
        )

    turn_total, turn_ok, turn_sum, turn_min, turn_max, exec_ok, exec_failed, watermark = ledger
    stats_spans = live_edge_spans(plan, watermark, from_, to)
    token_spans = live_edge_spans(plan, token_watermark, from_, to)
    distribution_window = _inspect_pg.retained_live_window(from_=from_, to=to, freeze=freeze)
    activity_window = _inspect_pg.retained_live_window(from_=from_, to=to, freeze=freeze)
    lifecycle_window = _inspect_pg.retained_live_window(from_=None, to=None, freeze=freeze)
    live = _projected_live_values(
        agent_id,
        stats_spans=stats_spans,
        token_spans=token_spans,
        distribution_window=distribution_window,
        activity_window=activity_window,
        lifecycle_window=lifecycle_window,
    )
    distribution = _turn_distribution(archive_distribution, live.distribution)
    minimum = min(
        (value for value in (turn_min, *live.turn_durations) if value is not None), default=0.0
    )
    maximum = max(
        (value for value in (turn_max, *live.turn_durations) if value is not None),
        default=0.0,
    )
    stats = AgentStats(
        turn_total=turn_total + live.turn_total,
        turn_ok=turn_ok + live.turn_ok,
        turn_p50_seconds=round(loki_events._weighted_quantile(0.5, distribution), 2),
        turn_p90_seconds=round(loki_events._weighted_quantile(0.9, distribution), 2),
        turn_min_seconds=round(minimum, 2),
        turn_max_seconds=round(maximum, 2),
        exec_ok=exec_ok + live.exec_ok,
        exec_failed=exec_failed + live.exec_failed,
    )
    return InspectValues(
        stats=stats,
        turn_duration_seconds=turn_sum + sum(live.turn_durations),
        output_tokens=ledger_tokens + int(live.output_tokens),
        active_seconds=archive_active_seconds + live.active_seconds,
        exec_seconds=archive_exec_seconds + live.exec_seconds,
        lifecycle_events=sorted([*archive_lifecycle, *live.lifecycle_events]),
    )


def stats_values(
    pool: ConnectionPool[Any], agent_id: int, from_: datetime | None, to: datetime | None
) -> StatsValues:
    """Compatibility wrapper for callers that need only the stats section."""
    values = inspect_values(pool, agent_id, from_, to)
    return StatsValues(stats=values.stats, turn_duration_seconds=values.turn_duration_seconds)


def agent_stats(
    pool: ConnectionPool[Any], agent_id: int, from_: datetime | None, to: datetime | None
) -> AgentStats:
    """Public stats section contract; internal consumers use the shared duration too."""
    return stats_values(pool, agent_id, from_, to).stats
