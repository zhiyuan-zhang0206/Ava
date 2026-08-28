"""Event-driven run timeline — ``GET /api/agents/{agent_id}/run-timeline``.

The timeline deliberately consumes only the unified Loki event stream.  It
therefore works for both per-turn and session-root tracing shapes: a
``turn_end`` row is the turn skeleton and its matching ``llm_usage.span_id``
supplies the token/cost measurement.  Tempo remains an optional call-level
drill-down source rather than a requirement for this run-level surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Annotated, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from gateway import loki_events
from gateway.routers._backend_failure import raise_backend_unavailable
from gateway.routers._eval_guard import deny_isolated_result_read
from gateway.schemas.run_timeline import (
    RunTimelineBoundaries,
    RunTimelineEvent,
    RunTimelineExec,
    RunTimelineLlm,
    RunTimelineMeta,
    RunTimelineResponse,
    RunTimelineRow,
    RunTimelineWindow,
)

router = APIRouter()

_RETENTION = timedelta(days=7)
_FALLBACK_WINDOW = timedelta(hours=24)
_ASSOCIATION_TOLERANCE = timedelta(seconds=2)
_PAGE_SIZE = 1_000
_BUCKET_PATTERN = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<unit>[smhd])$")

_TURN_EVENTS = (
    "llm_usage",
    "turn_end",
    "exec",
    "exec_failed",
    "exec(failed)",
    "exec_timeout",
    "exec(timeout)",
    "exec_cancelled",
    "exec(cancelled)",
    "exec_node_timeout",
    "exec_thread_stuck",
    "halt",
    "compact",
    "auto_compact",
    "agent_spawned",
    "spawn",
    "agent_resurrected",
    "resurrect",
    "agent_terminated",
    "terminate",
    "restart",
    "agent_restarted",
    "restart_completed",
    "idle_wake",
    "heartbeat_paused",
    "llm_provider_error",
    "stream_stalled_retry",
    "stream_overloaded_retry",
    "llm_turn_aborted",
)
_SESSION_START_EVENTS = frozenset(
    {
        "agent_spawned",
        "spawn",
        "agent_resurrected",
        "resurrect",
        "agent_restarted",
        "restart_completed",
    }
)
_COMPACT_EVENTS = frozenset({"compact", "auto_compact"})
# The audit family gives one event per user-visible restart/resurrection.  The
# matching telemetry events are deliberately not rail markers or meta counts.
_RESTART_EVENTS = frozenset({"restart_completed", "resurrect"})
_EXEC_EVENTS = frozenset(
    {
        "exec",
        "exec_failed",
        "exec(failed)",
        "exec_timeout",
        "exec(timeout)",
        "exec_cancelled",
        "exec(cancelled)",
        "exec_node_timeout",
        "exec_thread_stuck",
    }
)
_ANOMALY_EVENTS = frozenset(
    {
        "exec_failed",
        "exec(failed)",
        "exec_timeout",
        "exec(timeout)",
        "exec_cancelled",
        "exec(cancelled)",
        "exec_node_timeout",
        "exec_thread_stuck",
        "llm_provider_error",
        "stream_stalled_retry",
        "stream_overloaded_retry",
        "llm_turn_aborted",
    }
)
_RAIL_EVENTS = (
    _COMPACT_EVENTS | _RESTART_EVENTS | _EXEC_EVENTS | {"halt", "agent_terminated", "terminate"}
)


@dataclass(frozen=True)
class TurnTimelineAggregate:
    """The agent-independent portion of one timeline response."""

    meta: RunTimelineMeta
    rows: list[RunTimelineRow]
    events: list[RunTimelineEvent]
    boundaries: RunTimelineBoundaries


@dataclass(frozen=True)
class _TurnWindow:
    """A completed turn's time window for associating unkeyed child events."""

    turn: int
    event: dict[str, object]
    start: datetime
    end: datetime


def _number(value: object) -> float:
    """Read a Loki JSON number without letting malformed historical rows break a run."""
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _integer(value: object) -> int:
    return int(_number(value))


def _attrs(event: dict[str, object]) -> dict[str, object]:
    attrs = event["attributes"]
    return cast(dict[str, object], attrs) if isinstance(attrs, dict) else {}


def _event_ts(event: dict[str, object]) -> datetime:
    ts = event["ts"]
    if not isinstance(ts, datetime):
        raise TypeError(f"Loki event ts must be datetime, got {type(ts)!r}")
    return ts


def _event_name(event: dict[str, object]) -> str:
    name = event["event_name"]
    if not isinstance(name, str):
        raise TypeError(f"Loki event_name must be str, got {type(name)!r}")
    return name


def _event_string(event: dict[str, object], key: str) -> str | None:
    value = event[key]
    return value if isinstance(value, str) else None


def _llm_usage(events: list[dict[str, object]]) -> RunTimelineLlm:
    models = {
        model
        for event in events
        if isinstance((model := _attrs(event).get("model")), str) and model
    }
    return RunTimelineLlm(
        calls=len(events),
        in_total=sum(_integer(_attrs(event).get("in_total")) for event in events),
        cache_read=sum(_integer(_attrs(event).get("cache_read")) for event in events),
        out_total=sum(_integer(_attrs(event).get("out_total")) for event in events),
        reasoning=sum(_integer(_attrs(event).get("reasoning")) for event in events),
        latency_ms=sum(_number(_attrs(event).get("latency_ms")) for event in events),
        cost_usd=sum(_number(_attrs(event).get("cost_usd")) for event in events),
        model=next(iter(models)) if len(models) == 1 else "multiple" if models else None,
    )


def _execution_events(events: list[dict[str, object]]) -> list[RunTimelineExec]:
    executions: list[RunTimelineExec] = []
    for event in events:
        name = _event_name(event)
        if name not in _EXEC_EVENTS:
            continue
        attrs = _attrs(event)
        tool = attrs.get("tool")
        executions.append(
            RunTimelineExec(
                tool=tool if isinstance(tool, str) and tool else "execute_code",
                dur_s=_number(attrs.get("duration_seconds", attrs.get("duration_s"))),
                ok=name == "exec",
            )
        )
    return executions


def _turn_window(turn: int, turn_end: dict[str, object]) -> _TurnWindow:
    attrs = _attrs(turn_end)
    end = _event_ts(turn_end)
    duration_s = max(0.0, _number(attrs.get("duration_seconds")))
    return _TurnWindow(
        turn=turn,
        event=turn_end,
        start=end - timedelta(seconds=duration_s),
        end=end,
    )


def _assign_events_by_time(
    turns: list[_TurnWindow], events: list[dict[str, object]]
) -> list[list[dict[str, object]]]:
    """Attach ordered events once, preferring their containing turn window.

    A session-root trace id is shared by sibling turns, so it cannot join child
    events to a turn.  The event stream is chronological: after an event falls
    between turn windows, the next completed turn is the only stable fallback.
    """
    assignments: list[list[dict[str, object]]] = [[] for _ in turns]
    turn_index = 0
    for event in sorted(events, key=_event_ts):
        event_ts = _event_ts(event)
        while turn_index < len(turns) and turns[turn_index].end < event_ts:
            turn_index += 1
        if turn_index == len(turns):
            if turns:
                # The event belongs to an in-flight next turn whose turn_end is
                # not visible yet. Keep it once on the final completed row so
                # the selected window does not silently drop execution evidence.
                assignments[-1].append(event)
            continue

        candidate = turns[turn_index]
        if candidate.start - _ASSOCIATION_TOLERANCE <= event_ts <= candidate.end:
            assignments[turn_index].append(event)
            continue

        # The event is in a gap (or predates the first visible turn).  Associate
        # it with the smallest later turn_end rather than leaking it across rows.
        assignments[turn_index].append(event)
    return assignments


def _associate_llm_usage(
    turns: list[_TurnWindow], usages: list[dict[str, object]]
) -> tuple[list[list[dict[str, object]]], list[bool]]:
    """Prefer the exact span join, then recover span-less telemetry by time."""
    assignments: list[list[dict[str, object]]] = [[] for _ in turns]
    turns_by_span: dict[str, int] = {}
    for index, turn in enumerate(turns):
        span_id = _event_string(turn.event, "span_id")
        if span_id is not None:
            turns_by_span.setdefault(span_id, index)

    unjoined: list[dict[str, object]] = []
    for usage in sorted(usages, key=_event_ts):
        span_id = _event_string(usage, "span_id")
        turn_index = turns_by_span.get(span_id) if span_id is not None else None
        if turn_index is None:
            unjoined.append(usage)
        else:
            assignments[turn_index].append(usage)

    fallback_usage_by_turn = _assign_events_by_time(turns, unjoined)
    for turn_index, fallback_usages in enumerate(fallback_usage_by_turn):
        assignments[turn_index].extend(fallback_usages)
    return assignments, [bool(usages) for usages in fallback_usage_by_turn]


def _row_for_turn(
    turn: int,
    turn_end: dict[str, object],
    usages: list[dict[str, object]],
    associated_events: list[dict[str, object]],
) -> RunTimelineRow:
    attrs = _attrs(turn_end)
    end = _event_ts(turn_end)
    duration_s = max(0.0, _number(attrs.get("duration_seconds")))
    start = end - timedelta(seconds=duration_s)
    if duration_s == 0 and usages:
        start = min(_event_ts(event) for event in usages)

    llm = _llm_usage(usages)
    execs = _execution_events(associated_events)
    active_s = min(duration_s, llm.latency_ms / 1000 + sum(exec_.dur_s for exec_ in execs))
    anomalies = [
        _event_name(event) for event in associated_events if _event_name(event) in _ANOMALY_EVENTS
    ]
    ok = attrs.get("ok")
    if ok is False:
        anomalies.append("turn_end_failed")
    checkpoint_id = attrs.get("checkpoint_id")
    return RunTimelineRow(
        turn=turn,
        n_turns=1,
        start=start,
        end=end,
        active_s=active_s,
        trace_id=_event_string(turn_end, "trace_id"),
        checkpoint_id=checkpoint_id if isinstance(checkpoint_id, str) else None,
        ok=ok if isinstance(ok, bool) else None,
        llm=llm,
        execs=execs,
        anomalies=sorted(set(anomalies)),
        tags=[],
    )


def _bucket_rows(
    rows: list[RunTimelineRow], window_start: datetime, bucket_seconds: int
) -> list[RunTimelineRow]:
    groups: dict[int, list[RunTimelineRow]] = {}
    for row in rows:
        index = max(0, int((row.start - window_start).total_seconds() // bucket_seconds))
        groups.setdefault(index, []).append(row)

    bucketed: list[RunTimelineRow] = []
    for index, group in sorted(groups.items()):
        llm_events = [row.llm for row in group]
        models = {llm.model for llm in llm_events if llm.model is not None}
        bucketed.append(
            RunTimelineRow(
                turn=None,
                n_turns=sum(row.n_turns for row in group),
                start=window_start + timedelta(seconds=index * bucket_seconds),
                end=max(row.end for row in group),
                active_s=sum(row.active_s for row in group),
                trace_id=None,
                checkpoint_id=None,
                ok=all(row.ok is True for row in group),
                llm=RunTimelineLlm(
                    calls=sum(llm.calls for llm in llm_events),
                    in_total=sum(llm.in_total for llm in llm_events),
                    cache_read=sum(llm.cache_read for llm in llm_events),
                    out_total=sum(llm.out_total for llm in llm_events),
                    reasoning=sum(llm.reasoning for llm in llm_events),
                    latency_ms=sum(llm.latency_ms for llm in llm_events),
                    cost_usd=sum(llm.cost_usd for llm in llm_events),
                    model=next(iter(models))
                    if len(models) == 1
                    else "multiple"
                    if models
                    else None,
                ),
                execs=[exec_ for row in group for exec_ in row.execs],
                anomalies=sorted({anomaly for row in group for anomaly in row.anomalies}),
                tags=sorted({tag for row in group for tag in row.tags}),
            )
        )
    return bucketed


def _assign_marker_tags(rows: list[RunTimelineRow], events: list[dict[str, object]]) -> None:
    for event in events:
        name = _event_name(event)
        if name not in _COMPACT_EVENTS | {"idle_wake", "heartbeat_paused"}:
            continue
        event_ts = _event_ts(event)
        candidates = [row for row in rows if row.end <= event_ts]
        if name == "idle_wake":
            candidates = [row for row in rows if row.start >= event_ts] or candidates
        if not candidates:
            continue
        target = candidates[-1] if name in _COMPACT_EVENTS else candidates[0]
        target.tags.append(f"{name}@{event_ts.isoformat()}")

    for previous, current in pairwise(rows):
        idle_s = (current.start - previous.end).total_seconds()
        if idle_s > 0:
            current.tags.append(f"idle_before_{round(idle_s)}s")


def _rail_events(events: list[dict[str, object]]) -> list[RunTimelineEvent]:
    rail: list[RunTimelineEvent] = []
    for event in events:
        name = _event_name(event)
        if name not in _RAIL_EVENTS:
            continue
        attrs = _attrs(event)
        label = attrs.get("exc_type") or attrs.get("reason") or attrs.get("body")
        rail.append(
            RunTimelineEvent(
                ts=_event_ts(event),
                kind=name,
                trace_id=_event_string(event, "trace_id"),
                label=label if isinstance(label, str) else None,
            )
        )
    return rail


def aggregate_turn_timeline(
    events: list[dict[str, object]],
    window_start: datetime,
    window_end: datetime,
    *,
    bucket_seconds: int | None = None,
) -> TurnTimelineAggregate:
    """Build ordered turn rows from an event slice, optionally time-bucketed."""
    ordered = sorted(events, key=_event_ts)
    turns = [
        _turn_window(turn, turn_end)
        for turn, turn_end in enumerate(
            (event for event in ordered if _event_name(event) == "turn_end"), start=1
        )
    ]
    usages_by_turn, used_usage_fallback = _associate_llm_usage(
        turns, [event for event in ordered if _event_name(event) == "llm_usage"]
    )
    associated_events_by_turn = _assign_events_by_time(
        turns,
        [
            event
            for event in ordered
            if _event_name(event) in _EXEC_EVENTS | (_ANOMALY_EVENTS - _EXEC_EVENTS)
        ],
    )

    rows: list[RunTimelineRow] = []
    for index, turn in enumerate(turns):
        rows.append(
            _row_for_turn(
                turn.turn,
                turn.event,
                usages_by_turn[index],
                associated_events_by_turn[index],
            )
        )

    _assign_marker_tags(rows, ordered)
    compact_events = [event for event in ordered if _event_name(event) in _COMPACT_EVENTS]
    last_compact = compact_events[-1] if compact_events else None
    last_before_compact = (
        next((row.turn for row in reversed(rows) if row.end <= _event_ts(last_compact)), None)
        if last_compact is not None
        else None
    )
    turn_rows = rows
    rows = _bucket_rows(rows, window_start, bucket_seconds) if bucket_seconds is not None else rows

    llm_rows = [row.llm for row in turn_rows]
    meta = RunTimelineMeta(
        n_turns=len(turn_rows),
        wall_span_s=max(0.0, (window_end - window_start).total_seconds()),
        active_s=sum(row.active_s for row in turn_rows),
        tokens_in=sum(row.in_total for row in llm_rows),
        tokens_out=sum(row.out_total for row in llm_rows),
        cost_usd=sum(row.cost_usd for row in llm_rows),
        n_exec_failed=sum(1 for event in ordered if _event_name(event) in _EXEC_EVENTS - {"exec"}),
        n_compact=len(compact_events),
        n_restart=sum(1 for event in ordered if _event_name(event) in _RESTART_EVENTS),
        fallback_turns=sum(used_usage_fallback),
        unmatched_turns=sum(1 for row in turn_rows if row.llm.calls == 0),
    )
    return TurnTimelineAggregate(
        meta=meta,
        rows=rows,
        events=_rail_events(ordered),
        boundaries=RunTimelineBoundaries(
            initialize_turn=turn_rows[0].turn if turn_rows else None,
            last_before_compact_turn=last_before_compact,
            post_window_turns=0,
            has_activity_after_window=False,
        ),
    )


def _query_all_events(
    agent_id: int,
    from_: datetime,
    to: datetime,
    *,
    event_names: tuple[str, ...] = _TURN_EVENTS,
) -> list[dict[str, object]]:
    """Read every relevant event page so turn aggregation is never page-truncated."""
    events: list[dict[str, object]] = []
    offset = 0
    while True:
        page, has_more = loki_events.query_events(
            agent_id=agent_id,
            event_names=list(event_names),
            from_=from_,
            to=to,
            limit=_PAGE_SIZE,
            offset=offset,
            direction="forward",
        )
        events.extend(page)
        if not has_more:
            return events
        offset += _PAGE_SIZE


def _default_window(
    agent_id: int, now: datetime, *, session: Literal["compact", "current"]
) -> tuple[datetime, datetime]:
    """Choose the latest compact-ended or current observable session.

    Loki retains seven days. ``compact`` ends at the latest compact, while
    ``current`` runs from the latest lifecycle start to ``now``. Agents whose
    lifecycle predates retention use a bounded last-24-hours view instead of an
    unbounded scan.
    """
    retention_start = now - _RETENTION
    lifecycle_events = _query_all_events(
        agent_id,
        retention_start,
        now,
        event_names=tuple(_SESSION_START_EVENTS | _COMPACT_EVENTS),
    )
    ordered = sorted(lifecycle_events, key=_event_ts)
    starts = [_event_ts(event) for event in ordered if _event_name(event) in _SESSION_START_EVENTS]
    if session == "current":
        return (starts[-1], now) if starts else (now - _FALLBACK_WINDOW, now)

    compacts = [event for event in ordered if _event_name(event) in _COMPACT_EVENTS]
    if compacts:
        end = _event_ts(compacts[-1])
        starts = [
            _event_ts(event)
            for event in ordered
            if _event_name(event) in _SESSION_START_EVENTS and _event_ts(event) <= end
        ]
        return (starts[-1] if starts else max(retention_start, end - _FALLBACK_WINDOW), end)

    if starts:
        return starts[-1], now
    return now - _FALLBACK_WINDOW, now


def _parse_bucket_seconds(bucket: str | None) -> int:
    if bucket is None:
        raise HTTPException(status_code=422, detail="bucket is required when level=bucket")
    match = _BUCKET_PATTERN.fullmatch(bucket)
    if match is None:
        raise HTTPException(
            status_code=422, detail="bucket must use a positive s, m, h, or d suffix"
        )
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match["unit"]]
    return int(match["count"]) * multiplier


def _effective_window(
    agent_id: int,
    from_: datetime | None,
    to: datetime | None,
    now: datetime,
    *,
    session: Literal["compact", "current"],
) -> tuple[datetime, datetime]:
    for name, value in (("from", from_), ("to", to)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(status_code=422, detail=f"{name} must include a timezone offset")
    if from_ is None and to is None:
        return _default_window(agent_id, now, session=session)
    end = to or now
    start = from_ or end - _FALLBACK_WINDOW
    if start >= end:
        raise HTTPException(status_code=422, detail="from must be earlier than to")
    return start, end


@router.get(
    "/api/agents/{agent_id}/run-timeline",
    dependencies=[Depends(deny_isolated_result_read)],
)
def get_run_timeline(
    agent_id: int,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    level: Annotated[Literal["turn", "bucket"], Query()] = "turn",
    bucket: Annotated[str | None, Query()] = None,
    session: Annotated[Literal["compact", "current"], Query()] = "compact",
) -> RunTimelineResponse:
    """Return an event-driven session waterfall with turn or bucket rows."""
    now = datetime.now(UTC)
    window_start, window_end = _effective_window(agent_id, from_, to, now, session=session)
    try:
        events = _query_all_events(agent_id, window_start, window_end)
        post_window_events = (
            _query_all_events(
                agent_id,
                window_end,
                now,
                event_names=tuple(_SESSION_START_EVENTS | {"turn_end"}),
            )
            if session == "compact" and from_ is None and to is None and window_end < now
            else []
        )
    except httpx.HTTPError as exc:
        raise_backend_unavailable(exc)

    aggregate = aggregate_turn_timeline(
        events,
        window_start,
        window_end,
        bucket_seconds=_parse_bucket_seconds(bucket) if level == "bucket" else None,
    )
    post_window_turns = sum(
        1
        for event in post_window_events
        if _event_name(event) == "turn_end" and _event_ts(event) > window_end
    )
    has_activity_after_window = post_window_turns > 0 or any(
        _event_name(event) in _SESSION_START_EVENTS and _event_ts(event) > window_end
        for event in post_window_events
    )
    return RunTimelineResponse(
        agent_id=agent_id,
        window=RunTimelineWindow(from_=window_start, to=window_end),
        meta=aggregate.meta,
        rows=aggregate.rows,
        events=aggregate.events,
        boundaries=RunTimelineBoundaries(
            initialize_turn=aggregate.boundaries.initialize_turn,
            last_before_compact_turn=aggregate.boundaries.last_before_compact_turn,
            post_window_turns=post_window_turns,
            has_activity_after_window=has_activity_after_window,
        ),
    )
