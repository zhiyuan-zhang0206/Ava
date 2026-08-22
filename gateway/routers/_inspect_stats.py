"""Window planning and consolidated bounded-Loki turn stats for inspect."""

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


class _LokiStats(NamedTuple):
    """The three consolidated live queries merged across bounded shards."""

    turn_total: int
    turn_ok: int
    exec_ok: int
    exec_failed: int
    turn_duration_seconds: float
    turn_min_seconds: float | None
    turn_max_seconds: float | None


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
    """Coalesce overlapping raw spans before their shard fan-out."""
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


def _loki_stats(agent_id: int, spans: list[tuple[datetime, datetime]]) -> _LokiStats:
    """Run the inspect stats' three Loki query shapes over <=3h shards."""
    count_rows: list[dict[str, int]] = []
    ok_rows: list[dict[str, int]] = []
    distributions: list[list[tuple[float, int]]] = []

    def count_query(start: datetime, end: datetime) -> dict[str, int]:
        return loki_events.count_by_event_name(
            agent_id=agent_id,
            event_names=["^turn_end$", "^exec$", "^exec_.*", "^exec\\(.*"],
            categories=["telemetry", "log"],
            attribute_filters=None,
            from_=start,
            to=end,
        )

    def ok_query(start: datetime, end: datetime) -> dict[str, int]:
        return loki_events.count_by_event_name(
            agent_id=agent_id,
            event_names=["^turn_end$"],
            categories=["telemetry", "log"],
            attribute_filters={"ok": "true"},
            from_=start,
            to=end,
        )

    def duration_query(start: datetime, end: datetime) -> list[tuple[float, int]]:
        return loki_events.attribute_distribution(
            field="duration_seconds",
            agent_id=agent_id,
            event_names=["^turn_end$"],
            categories=["telemetry", "log"],
            attribute_filters=None,
            from_=start,
            to=end,
        )

    for start, end in spans:
        count_rows.extend(_inspect_pg.query_loki_shards(start, end, count_query))
        ok_rows.extend(_inspect_pg.query_loki_shards(start, end, ok_query))
        distributions.extend(_inspect_pg.query_loki_shards(start, end, duration_query))

    counts: dict[str, int] = {}
    for row in count_rows:
        for event_name, count in row.items():
            counts[event_name] = counts.get(event_name, 0) + count
    turn_ok = sum(row.get("turn_end", 0) for row in ok_rows)
    flat_distribution = [entry for distribution in distributions for entry in distribution]
    return _LokiStats(
        turn_total=counts.get("turn_end", 0),
        turn_ok=turn_ok,
        exec_ok=counts.get("exec", 0),
        exec_failed=sum(
            count for event_name, count in counts.items() if event_name not in {"turn_end", "exec"}
        ),
        turn_duration_seconds=sum(value * count for value, count in flat_distribution),
        turn_min_seconds=min((value for value, _ in flat_distribution), default=None),
        turn_max_seconds=max((value for value, _ in flat_distribution), default=None),
    )


def _turn_distribution(
    pool: ConnectionPool[Any], agent_id: int, from_: datetime | None, to: datetime | None
) -> list[tuple[float, int]]:
    """Stitch the exact frozen archive distribution to retained live buckets.

    The 2026-08-13 → 2026-08-16 cutover seam has no source rows: the archive
    ended and Loki later pruned before mirroring resumed. It remains absent
    from this historical percentile rather than being fabricated or bridged.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        freeze = _inspect_pg.freeze_point(cur)
        archive = _inspect_pg.archive_distribution(
            conn,
            field="duration_seconds",
            agent_id=agent_id,
            event_names=["^turn_end$"],
            categories=["telemetry", "log"],
            attribute_filters=None,
            from_=from_,
            to=to,
        )
    live_window = _inspect_pg.retained_live_window(from_=from_, to=to, freeze=freeze)
    live: list[list[tuple[float, int]]] = []
    if live_window is not None:
        start, end = live_window
        live = _inspect_pg.query_loki_shards(
            start,
            end,
            lambda shard_start, shard_end: loki_events.attribute_distribution(
                field="duration_seconds",
                agent_id=agent_id,
                event_names=["^turn_end$"],
                categories=["telemetry", "log"],
                attribute_filters=None,
                from_=shard_start,
                to=shard_end,
            ),
        )
    # Reuse Loki's private implementation deliberately: its interpolation is
    # the existing percentile_cont contract for inspector duration metrics.
    # Bucketing is only needed when archive and live data meet at the seam;
    # a window with one source preserves its exact duration values.
    return (
        merge_distribution([archive, *live])
        if archive and any(live)
        else merge_exact_distribution([archive, *live])
    )


def stats_values(
    pool: ConnectionPool[Any], agent_id: int, from_: datetime | None, to: datetime | None
) -> StatsValues:
    """Turn/exec stats from day ledger + bounded edges, with stitched percentiles."""
    plan = full_day_plan(from_, to)
    if plan.has_full_days:
        with pool.connection() as conn:
            ledger = _inspect_pg.ledger_stats(
                conn, agent_id=agent_id, day_from=plan.day_from, day_to=plan.day_to
            )
    else:
        ledger = (0, 0, 0.0, None, None, 0, 0, None)
    turn_total, turn_ok, turn_sum, turn_min, turn_max, exec_ok, exec_failed, watermark = ledger
    live = _loki_stats(agent_id, live_edge_spans(plan, watermark, from_, to))
    distribution = _turn_distribution(pool, agent_id, from_, to)
    minimum = min(
        (value for value in (turn_min, live.turn_min_seconds) if value is not None), default=0.0
    )
    maximum = max(
        (value for value in (turn_max, live.turn_max_seconds) if value is not None), default=0.0
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
    return StatsValues(stats=stats, turn_duration_seconds=turn_sum + live.turn_duration_seconds)


def agent_stats(
    pool: ConnectionPool[Any], agent_id: int, from_: datetime | None, to: datetime | None
) -> AgentStats:
    """Public stats section contract; internal consumers use the shared duration too."""
    return stats_values(pool, agent_id, from_, to).stats
