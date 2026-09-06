"""Window planning and consolidated bounded-Loki turn stats for inspect.

When the newest metrics-ledger day remains inside Loki retention, inspect
excludes that potentially stale day from the ledger and rereads it entirely
from live events. Late writes into a closed day are therefore neither lost nor
double counted.
"""

from __future__ import annotations

import threading
import time as time_mod
from datetime import UTC, date, datetime, time, timedelta
from math import floor
from typing import Any, NamedTuple, cast

from psycopg_pool import ConnectionPool

from gateway import loki_events
from gateway.routers import _inspect_pg
from gateway.routers._agent_cost import _INSPECT_QUERY_TIMEOUT_S, _query_timeout
from gateway.routers._inspect_cache import InspectQueryCache
from gateway.schemas import AgentStats
from shared.loki_index_labels import INDEX_LABEL_CUTOVER_AT, ledger_gap_plan, retention_floor


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


_LIFECYCLE_EVENTS = ("agent_spawned", "agent_resurrected", "agent_terminated")
_LIFECYCLE_EVENT_PATTERNS = [f"^{event}$" for event in _LIFECYCLE_EVENTS]
_LIFECYCLE_CACHE_TTL_S = 1800.0
_lifecycle_cache = InspectQueryCache[int, list[tuple[datetime, str]]](
    max_entries=256,
    max_inflight=128,
)
_lifecycle_cache_freeze: datetime | None | object = object()
_lifecycle_cache_lock = threading.Lock()


def reset_for_tests() -> None:
    """Clear the per-agent lifecycle cache between isolated test databases."""
    global _lifecycle_cache_freeze  # noqa: PLW0603 — intentional test seam
    with _lifecycle_cache_lock:
        _lifecycle_cache.clear()
        _lifecycle_cache_freeze = object()


def _load_live_lifecycle(
    agent_id: int,
    window: tuple[datetime, datetime],
    *,
    timeout_s: float | None = None,
) -> list[tuple[datetime, str]]:
    """Read one agent's indexed retained lifecycle leg within its query bound."""
    rows = loki_events.query_projected_lines(
        fields=[],
        template="{{ __line__ }}",
        agent_id=agent_id,
        event_names=_LIFECYCLE_EVENT_PATTERNS,
        from_=window[0],
        to=window[1],
        timeout_s=timeout_s,
    )
    events: list[tuple[datetime, str]] = []
    seen: set[tuple[int, str]] = set()
    for ts_ns, _row_agent_id, line in rows:
        key = (ts_ns, line)
        if key in seen:
            continue
        seen.add(key)
        row = loki_events._parse_line(line, ts_ns)
        if row is not None and row["event_name"] in _LIFECYCLE_EVENTS:
            events.append((row["ts"], row["event_name"]))
    return events


def cached_live_lifecycle(
    agent_id: int,
    *,
    window: tuple[datetime, datetime] | None,
    freeze: datetime | None,
    timeout_s: float | None = None,
) -> list[tuple[datetime, str]]:
    """Return the once-per-agent indexed lifecycle leg for about thirty minutes.

    The retained window is independent of the requested inspector window, so
    one 8-second-bounded cold load serves every `hours` and `since_compact`
    view for this agent. Lifecycle events are rare; a fresh
    spawn/resurrect/terminate can lag the panel by up to thirty minutes, versus
    its existing 75-second aggregate cache. When the archive freeze changes
    (only test reset in practice), cached live windows are discarded.
    """
    if window is None:
        return []
    global _lifecycle_cache_freeze  # noqa: PLW0603 — cache epoch marker
    with _lifecycle_cache_lock:
        if _lifecycle_cache_freeze != freeze:
            _lifecycle_cache.clear()
            _lifecycle_cache_freeze = freeze
    load_timeout_s = _INSPECT_QUERY_TIMEOUT_S if timeout_s is None else timeout_s
    return _lifecycle_cache.get_or_load(
        agent_id,
        lambda: _load_live_lifecycle(agent_id, window, timeout_s=load_timeout_s),
        ttl_s=_LIFECYCLE_CACHE_TTL_S,
        now=time_mod.monotonic,
    )


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
    floor_at = retention_floor()
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


def _node_exit_durations(
    attributes: dict[str, Any], legacy_duration: float | None
) -> tuple[float, float]:
    """Return active/exec seconds from aggregate or retained legacy rows."""
    nodes = attributes.get("nodes")
    entries: list[dict[str, Any]]
    if isinstance(nodes, list):
        entries = [cast(dict[str, Any], entry) for entry in nodes if isinstance(entry, dict)]
    elif legacy_duration is not None:
        entries = [attributes]
    else:
        return 0.0, 0.0

    active_seconds = exec_seconds = 0.0
    for entry in entries:
        duration = _attribute_number(entry, "duration_seconds")
        if duration is None:
            continue
        node = _attribute_text(entry, "node")
        if node != "claim":
            active_seconds += duration
        if node == "exec":
            exec_seconds += duration
    return active_seconds, exec_seconds


def _projected_live_values(
    agent_id: int,
    *,
    stats_spans: list[tuple[datetime, datetime]],
    token_spans: list[tuple[datetime, datetime]],
    distribution_spans: list[tuple[datetime, datetime]],
    activity_window: tuple[datetime, datetime] | None,
    deadline: float | None = None,
) -> _LiveInspectValues:
    """Reduce one raw Loki line pass over every live interval inspect needs.

    The pass uses one envelope rather than one aggregation shape per interval.
    A row is still admitted to each ledger/archive seam only through that
    section's original span, preserving the existing double-count prevention.
    """
    spans = [*stats_spans, *token_spans, *distribution_spans]
    if activity_window is not None:
        spans.append(activity_window)
    if not spans:
        return _LiveInspectValues(0, 0, 0, 0, [], [], 0.0, 0.0, 0.0)

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
        ],
        from_=min(start for start, _ in spans),
        to=max(end for _, end in spans),
        timeout_s=_query_timeout(deadline),
        limit_per_slice=20000,
    )

    turn_total = turn_ok = exec_ok = exec_failed = 0
    turn_durations: list[float] = []
    distribution: list[tuple[float, int]] = []
    output_tokens = active_seconds = exec_seconds = 0.0
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
            and _in_spans(timestamp, distribution_spans)
        ):
            distribution.append((duration, 1))

        if telemetry_or_log and event_name == "llm_usage" and _in_spans(timestamp, token_spans):
            output_tokens += _attribute_number(attributes, "out_total") or 0.0

        if (
            event_name == "node_exit"
            and activity_window is not None
            and _in_spans(timestamp, [activity_window])
        ):
            active_delta, exec_delta = _node_exit_durations(attributes, duration)
            active_seconds += active_delta
            exec_seconds += exec_delta

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
    )


def _turn_distribution(
    archive: list[tuple[float, int]],
    ledger: list[tuple[float, int]],
    live: list[tuple[float, int]],
) -> list[tuple[float, int]]:
    """Merge the supplied bucketed and exact duration distributions."""
    return merge_exact_distribution([archive, ledger, live])


def _duration_bounds(
    *,
    histogram_complete: bool,
    ledger_min: float | None,
    ledger_max: float | None,
    archive_distribution: list[tuple[float, int]],
    live: _LiveInspectValues,
    distribution: list[tuple[float, int]],
) -> tuple[float, float]:
    """Return exact extrema without treating a floored histogram bucket as one."""
    if histogram_complete:
        values = [*[value for value, _ in archive_distribution], *live.turn_durations]
    else:
        values = [value for value, _ in distribution]
    return (
        min((value for value in (ledger_min, *values) if value is not None), default=0.0),
        max((value for value in (ledger_max, *values) if value is not None), default=0.0),
    )


def _distribution_sources(
    *,
    histogram_complete: bool,
    stats_spans: list[tuple[datetime, datetime]],
    ledger_distribution: list[tuple[float, int]],
    from_: datetime | None,
    to: datetime | None,
    freeze: datetime | None,
) -> tuple[list[tuple[datetime, datetime]], list[tuple[float, int]]]:
    """Return the live distribution spans and ledger buckets to merge.

    A complete daily histogram narrows the live distribution read to the
    stats edge spans; otherwise the retained live window is read raw and
    the ledger contributes no buckets.
    """
    if histogram_complete:
        return stats_spans, ledger_distribution
    distribution_window = _inspect_pg.retained_live_window(from_=from_, to=to, freeze=freeze)
    spans = [] if distribution_window is None else [distribution_window]
    return spans, []


def inspect_values(
    pool: ConnectionPool[Any],
    agent_id: int,
    from_: datetime | None,
    to: datetime | None,
    *,
    deadline: float | None = None,
) -> InspectValues:
    """Build stats, TPS, and activity inputs from one archive/ledger/live view.

    The retained live lifecycle stream is separately cached per agent for
    thirty minutes and begins at the index-label cutover, so it never scans
    the legacy slice. An agent with no indexed start event uses the existing
    `agents_meta.spawned_at → now` fallback; whole-life and wide-window views then
    approximate pre-cutover terminate/resurrect gaps until that slice expires
    on 2026-08-30. Whole-life archive distribution, activity, and lifecycle
    values use the frozen-archive rollup when it exists; windowed reads retain
    the raw archive fallback. Complete ledger days contribute integer-second
    duration buckets, making their percentiles accurate to about ±0.5 seconds;
    live-tail values remain exact. Every interactive Loki read is capped at
    eight seconds.
    """
    plan = full_day_plan(from_, to)
    ledger_distribution: list[tuple[float, int]] = []
    histogram_complete = False
    with pool.connection() as conn:
        freeze = _inspect_pg.FREEZE_AT
        rollup = _inspect_pg.archive_stats(conn, agent_id=agent_id)
        if plan.has_full_days:
            max_day = _inspect_pg.newest_ledger_day(
                conn, agent_id=agent_id, day_from=plan.day_from, day_to=plan.day_to
            )
            floor_at = retention_floor()
            gap = ledger_gap_plan(max_day, floor_at)
            ledger_day_to = plan.day_to if gap.day_lt is None else gap.day_lt - timedelta(days=1)
            ledger = _inspect_pg.ledger_stats(
                conn, agent_id=agent_id, day_from=plan.day_from, day_to=ledger_day_to
            )
            ledger_distribution, histogram_complete = _inspect_pg.ledger_distribution(
                conn, agent_id=agent_id, day_from=plan.day_from, day_to=ledger_day_to
            )
            histogram_complete = histogram_complete and bool(ledger_distribution)
            newest_tokens = _inspect_pg.newest_token_day(
                conn, agent_id=agent_id, day_from=plan.day_from, day_to=plan.day_to
            )
            token_gap = ledger_gap_plan(newest_tokens, floor_at)
            token_day_to = (
                plan.day_to if token_gap.day_lt is None else token_gap.day_lt - timedelta(days=1)
            )
            ledger_tokens, token_watermark = _inspect_pg.ledger_tokens(
                conn, agent_id=agent_id, day_from=plan.day_from, day_to=token_day_to
            )
        else:
            ledger = (0, 0, 0.0, None, None, 0, 0, None)
            ledger_tokens = 0
            token_watermark = None
        # The frozen-archive rollup (one row per agent, whole-life) serves
        # every whole-life read; the raw Loki archive-stream fallback covers
        # windowed reads and agents without a rollup row. Lifecycle comes
        # from the rollup whenever it exists (it is the same frozen data).
        if rollup is not None and from_ is None:
            archive_distribution = rollup.turn_distribution
            archive_active_seconds = rollup.active_seconds
            archive_exec_seconds = rollup.exec_seconds
            archive_lifecycle = rollup.lifecycle
        else:
            projection = _inspect_pg.archive_projected_values(
                agent_id=agent_id,
                from_=from_,
                to=to,
                timeout_s=_query_timeout(deadline),
            )
            archive_distribution = projection.turn_distribution
            archive_active_seconds = projection.active_seconds
            archive_exec_seconds = projection.exec_seconds
            archive_lifecycle = rollup.lifecycle if rollup is not None else projection.lifecycle

    turn_total, turn_ok, turn_sum, turn_min, turn_max, exec_ok, exec_failed, watermark = ledger
    stats_spans = live_edge_spans(plan, watermark, from_, to)
    token_spans = live_edge_spans(plan, token_watermark, from_, to)
    distribution_spans, ledger_duration_distribution = _distribution_sources(
        histogram_complete=histogram_complete,
        stats_spans=stats_spans,
        ledger_distribution=ledger_distribution,
        from_=from_,
        to=to,
        freeze=freeze,
    )
    activity_window = _inspect_pg.retained_live_window(from_=from_, to=to, freeze=freeze)
    lifecycle_window = _inspect_pg.retained_live_window(
        from_=INDEX_LABEL_CUTOVER_AT, to=None, freeze=freeze
    )
    if deadline is not None and deadline <= time_mod.monotonic():
        raise TimeoutError
    live = _projected_live_values(
        agent_id,
        stats_spans=stats_spans,
        token_spans=token_spans,
        distribution_spans=distribution_spans,
        activity_window=activity_window,
        deadline=deadline,
    )
    if deadline is not None and deadline <= time_mod.monotonic():
        raise TimeoutError
    live_lifecycle = cached_live_lifecycle(
        agent_id,
        window=lifecycle_window,
        freeze=freeze,
        timeout_s=_query_timeout(deadline),
    )
    distribution = _turn_distribution(
        [] if histogram_complete else archive_distribution,
        ledger_duration_distribution,
        live.distribution,
    )
    minimum, maximum = _duration_bounds(
        histogram_complete=histogram_complete,
        ledger_min=turn_min,
        ledger_max=turn_max,
        archive_distribution=archive_distribution,
        live=live,
        distribution=distribution,
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
        lifecycle_events=sorted([*archive_lifecycle, *live_lifecycle]),
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
