"""Run timeline — GET /api/agents/{id}/run-timeline.

The run-level read surface behind the timeline visualization: one agent's
event river (Loki) reassembled into run→turn→call rows, with a default
session-route window of `initialize → compact` (user ruling: the complete
session from context initialization to the latest compaction; adjustable via
`from`/`to`).

Data is events-only (zero new collection): `llm_usage` joins `turn_end` 1:1
by `span_id` (measured 519/520 in the design doc's data probe), `exec*`
events attach to turns by `trace_id`, `compact`/`auto_compact` mark the
session boundary, and the lifecycle events (`agent_spawned` /
`restart_completed`) mark the initialize side of the route. This makes the
endpoint naturally compatible with both per-turn-root and session-root span
shapes (the design doc's known caveat) — the turn skeleton comes from events,
never from span roots.

The response is bounded: `limit` (default 500, cap 2000) turn rows per page,
`offset` for paging; `meta.truncated` reports a hit cap so the frontend can
offer a narrower window.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, NamedTuple, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from gateway import loki_events, loki_query_budget
from gateway.routers._backend_failure import raise_backend_unavailable
from gateway.routers._eval_guard import deny_isolated_result_read
from gateway.schemas.run_timeline import (
    RunTimelineBoundaries,
    RunTimelineExec,
    RunTimelineLlm,
    RunTimelineMeta,
    RunTimelineResponse,
    RunTimelineRow,
)
from shared.db import agent_exists

router = APIRouter()

# Per-page row cap for the Loki event fetches (the events API's own limit
# ceiling is 1000; the run timeline pages in chunks of its own cap).
_EVENT_PAGE = 1000
# Hard cap on turn rows returned per request — bounds memory + render time.
_MAX_ROWS = 2000
# Default when the request names no `limit`.
_DEFAULT_ROWS = 500
# Lifecycle events that (re)initialize the agent context — the session
# route's start side.
_INITIALIZE_EVENTS = ("agent_spawned", "restart_completed")
# Compact events — the session route's end side.
_COMPACT_EVENTS = ("compact", "auto_compact")
# Exec-outcome events — call-level leaves (trace_id links them to a turn).
_EXEC_EVENTS = ("exec", "exec_failed", "exec_timeout")
# How far back the session-route probe looks for the latest compact when the
# request names no window (Loki retention is 168h — the probe is capped there).
_SESSION_PROBE_HOURS = 168
# Exec→turn attachment tolerance: session-root turn windows derive from
# turn_end.duration_seconds, which can sit a sub-second from the exec's own ts.
_EXEC_TOLERANCE = timedelta(seconds=5)


# Event rows arrive as `dict[str, Any]` from loki_events; strict pyright
# treats Any member access as unknown, so the assembly narrows every row to
# `dict[str, object]` once, then reads through the typed helpers below.
EventRow = dict[str, object]


def _attrs(row: EventRow) -> dict[str, object]:
    attrs = row.get("attributes")
    return cast(dict[str, object], attrs) if isinstance(attrs, dict) else {}


def _num(row: EventRow, key: str, default: float = 0.0) -> float:
    value = _attrs(row).get(key)
    return float(value) if isinstance(value, (int, float)) else default


def _int(row: EventRow, key: str, default: int = 0) -> int:
    return int(_num(row, key, float(default)))


def _str(row: EventRow, key: str, default: str = "") -> str:
    value = _attrs(row).get(key)
    return str(value) if isinstance(value, (str, int, float)) else default


def _bool(row: EventRow, key: str, *, default: bool) -> bool:
    value = _attrs(row).get(key)
    return bool(value) if isinstance(value, bool) else default


async def _fetch_events(
    *,
    agent_id: int,
    event_names: list[str],
    from_: datetime,
    to: datetime,
    limit: int = _EVENT_PAGE,
) -> list[dict[str, Any]]:
    """Fetch one event family over the window, paging until drained.

    Loki's query_range has no server-side offset; `query_events` pages in
    memory (`offset`). A window can hold more rows than one page (a busy agent
    emits one llm_usage + one turn_end per turn), so loop until `has_more`
    goes false or the family cap is hit.
    """
    rows: list[EventRow] = []
    offset = 0
    while True:
        try:
            page, has_more = await asyncio.to_thread(
                loki_events.query_events,
                agent_id=agent_id,
                event_names=event_names,
                from_=from_,
                to=to,
                limit=limit,
                offset=offset,
                direction="forward",
            )
            page_rows = [cast(EventRow, r) for r in page]
        except loki_query_budget.LokiQueryBudgetError:
            raise
        except httpx.HTTPError as exc:
            raise_backend_unavailable(exc)
        rows.extend(page_rows)
        if not has_more or len(rows) >= _MAX_ROWS * 4:
            break
        offset += limit
    return rows


def _llm_model(u: EventRow) -> RunTimelineLlm:
    attrs = _attrs(u)
    cost = attrs.get("cost_usd")
    return RunTimelineLlm(
        calls=_int(u, "calls", 1),
        in_total=_int(u, "in_total"),
        cache_read=_int(u, "cache_read"),
        out_total=_int(u, "out_total"),
        reasoning=_int(u, "reasoning"),
        latency_ms=_num(u, "latency_ms"),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        model=_str(u, "model"),
    )


class _AssembledTurn(NamedTuple):
    """One assembled turn before pydantic projection — the intermediate
    shape between raw event rows and RunTimelineRow."""

    start: datetime
    end: datetime
    active_s: float
    ok: bool
    trace_id: str
    llm: EventRow | None
    execs: list[EventRow]
    anomalies: list[str]
    warning: str | None = None


def _build_rows(
    usage: list[EventRow],
    turn_ends: list[EventRow],
    execs: list[EventRow],
    compacts: list[EventRow],
    initializes: list[EventRow],
    limit: int,
    offset: int,
    *,
    boundary_compact_at: datetime | None = None,
) -> tuple[list[RunTimelineRow], RunTimelineBoundaries, RunTimelineMeta]:
    """Assemble the turn skeleton from the event families.

    Join `llm_usage` ↔ `turn_end` on `span_id` (1:1); `exec*` attach by
    `trace_id`; compact events mark the boundary. Rows are ordered by turn
    start ascending; paging (`limit`/`offset`) applies to the assembled rows
    (stable order — same key the frontend pages with).
    """
    assembled, n_unpaired = _assemble_turns(usage, turn_ends, execs)
    compact_ts = _event_times(compacts)
    # The default session route ends AT the compact event, but Loki's
    # query_range end is exclusive — the in-window fetch misses it. The
    # probe already resolved it; carry it so the boundary row is tagged
    # and n_compact counts it (QA W4: the purple compact line must render).
    if boundary_compact_at is not None and boundary_compact_at not in compact_ts:
        compact_ts.append(boundary_compact_at)
    init_ts = _event_times(initializes)

    # Stable ascending order (same key as the frontend pages with).
    assembled.sort(key=lambda r: (r.start, r.end))

    total = len(assembled)
    page_rows = assembled[offset : offset + limit]
    truncated = total > offset + limit

    # Boundaries come from the FULL assembled set (not the paged slice) so
    # `initialize_at` stays the session's true first turn even on page 2+.
    boundaries = RunTimelineBoundaries(
        initialize_at=min((r.start for r in assembled), default=None),
        compact_at=max(compact_ts, default=None),
    )

    n_exec_failed = sum(1 for r in execs if r.get("event_name") == "exec_failed")
    n_restart = len(_dedupe_markers(init_ts))
    tokens_in = sum(_int(r.llm, "in_total") for r in assembled if r.llm is not None)
    tokens_out = sum(_int(r.llm, "out_total") for r in assembled if r.llm is not None)
    cost = sum(_num(r.llm, "cost_usd") for r in assembled if r.llm is not None)
    wall = (
        (max(r.end for r in assembled) - min(r.start for r in assembled)).total_seconds()
        if assembled
        else 0.0
    )
    meta = RunTimelineMeta(
        n_turns=total,
        wall_span_s=wall,
        active_s=sum(r.active_s for r in assembled),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        n_exec_failed=n_exec_failed,
        n_compact=len(compact_ts),
        n_restart=n_restart,
        truncated=truncated,
        warnings=_collect_warnings(n_unpaired, assembled, n_exec_failed),
    )

    rows = _project_rows(page_rows, compact_ts, init_ts, offset, total)
    return rows, boundaries, meta


def _event_times(rows: list[EventRow]) -> list[datetime]:
    """The event timestamps that are actually datetimes (defensive against
    a malformed row; the assembly filters rather than trusting the wire)."""
    times: list[datetime] = []
    for r in rows:
        ts = r.get("ts")
        if isinstance(ts, datetime):
            times.append(ts)
    return times


def _assemble_turns(
    usage: list[EventRow],
    turn_ends: list[EventRow],
    execs: list[EventRow],
) -> tuple[list[_AssembledTurn], int]:
    """One turn per `turn_end` event — the turn skeleton.

    The skeleton is **turn_end-driven**, not llm_usage-driven: `llm_usage`
    is logged only on a successful LLM call (agent/graph/_llm_stream.py —
    "A raised call never stamps"), so a failed/retried turn has a turn_end
    with ok=False and NO usage row. Skeletoning on usage would silently drop
    every failed turn — the red "failed turn" encoding would be dead code.
    Every turn_end forms a row; the usage row attaches when present.

    Join semantics follow the two observed tracing shapes (the design doc's
    known caveat):

    - **llm_usage → turn**: primary key is `span_id` (1:1, measured 519/520
      in the design probe). Events from the 2026-08-28 19:03Z+ process carry
      NEITHER span_id NOR trace_id — those attach by time containment (the
      usage ts inside the turn's window; closest turn_end wins), counted in
      the returned `n_unpaired` so the caller can surface a warning. A usage
      row that pairs with no turn still forms its own open row with tokens
      intact — tokens are NEVER silently zeroed.
    - **exec → turn**: assigned globally, one turn per exec, in this order:
      (1) span_id equality (per-turn-root shape); (2) time containment —
      exec.ts inside exactly one turn's [start, end] (the session-root
      shape, where exec spans are children of the turn root so the event's
      own span_id differs, and trace_id is shared across turns); (3)
      trace_id equality only when exactly one turn carries that trace.
      Execs that match nothing are counted in the returned
      `n_unattached_execs` (also surfaced as a warning) instead of silently
      disappearing.
    """
    ends_by_span: dict[str, EventRow] = {}
    for r in turn_ends:
        span = r.get("span_id")
        # Empty span_id (post-19:03Z shape) must NOT become a join key — it
        # would false-pair every span-less usage row with the first span-less
        # turn_end; those go through time containment instead.
        if isinstance(span, str) and span:
            ends_by_span[span] = r

    assembled: list[_AssembledTurn] = []
    used_ends: set[int] = set()
    n_unpaired = 0
    ends_by_ts: list[tuple[datetime, EventRow]] = []
    for r in turn_ends:
        ts = r.get("ts")
        if isinstance(ts, datetime):
            ends_by_ts.append((ts, r))

    for u in usage:
        span_id = u.get("span_id")
        end = ends_by_span.get(span_id) if isinstance(span_id, str) and span_id else None
        if end is None:
            # No span key — resolve by time containment: the usage ts inside
            # [turn_end - duration, turn_end], closest turn_end wins.
            u_ts = u.get("ts")
            if isinstance(u_ts, datetime):
                anchor_ts: datetime = u_ts  # closure-safe narrowed alias
                candidates: list[tuple[datetime, EventRow]] = []
                for end_ts, r in ends_by_ts:
                    if anchor_ts <= end_ts and end_ts - anchor_ts <= timedelta(seconds=3600):
                        candidates.append((end_ts, r))
                if candidates:
                    end = min(
                        candidates,
                        key=lambda p: abs((p[0] - anchor_ts).total_seconds()),
                    )[1]
        if end is not None:
            used_ends.add(id(end))
        turn, paired_by_time = _pair_turn_end(u, end)
        assembled.append(turn)
        if paired_by_time:
            n_unpaired += 1

    # Every turn_end NOT already claimed by a usage row forms its own turn —
    # including failed turns (ok=False, no usage). Ordering is by start.
    for r in turn_ends:
        if id(r) in used_ends:
            continue
        ts = r.get("ts")
        r_ts = ts if isinstance(ts, datetime) else datetime.now(UTC)
        start = min(r_ts - timedelta(seconds=_num(r, "duration_seconds")), r_ts)
        assembled.append(
            _AssembledTurn(
                start=start,
                end=r_ts,
                active_s=float((r_ts - start).total_seconds()),
                ok=_bool(r, "ok", default=True),
                trace_id=_str(r, "trace_id"),
                llm=None,
                execs=[],
                anomalies=[],
                warning=None,
            )
        )

    assembled.sort(key=lambda t: (t.start, t.end))
    exec_map, n_unattached = _assign_execs(execs, assembled)
    # Rebuild the list (NamedTuple._replace returns a new object — assigning
    # to the loop variable would discard it); warnings are computed from the
    # now-assigned execs.
    updated: list[_AssembledTurn] = []
    for turn in assembled:
        turn_execs = exec_map.get(id(turn), [])
        anomalies: list[str] = []
        for e in turn_execs:
            if e.get("event_name") not in ("exec_failed", "exec_timeout"):
                continue
            e_ts = e.get("ts")
            stamp = e_ts.strftime("%H:%M:%S") if isinstance(e_ts, datetime) else ""
            detail = f" {_str(e, 'exc_type')}" if e.get("event_name") == "exec_failed" else ""
            anomalies.append(f"{e.get('event_name')!s}@{stamp}{detail}")
        warning = turn.warning
        if n_unattached > 0 and id(turn) == id(assembled[-1]):
            warning = f"{warning + '; ' if warning else ''}{n_unattached} unattached exec(s)"
        updated.append(turn._replace(execs=turn_execs, anomalies=anomalies, warning=warning))
    return updated, n_unpaired


def _pair_turn_end(
    u: EventRow,
    end: EventRow | None,
) -> tuple[_AssembledTurn, bool]:
    """Pair one llm_usage row to its turn_end.

    Returns (turn, paired_by_time): `paired_by_time` is True when the
    turn_end was matched by time containment because the usage row carried
    no span_id (the post-2026-08-28 19:03Z process shape). The caller counts
    those as a data-quality warning; tokens are never dropped for an
    unpaired row — it still forms a turn row.
    """
    span_id = u.get("span_id")
    trace_id = u.get("trace_id")
    tid = trace_id if isinstance(trace_id, str) else ""
    ts = u.get("ts")
    u_ts = ts if isinstance(ts, datetime) else datetime.now(UTC)

    # Fallback for a span-less usage row: the caller pre-resolves `end` by
    # time containment (closest turn_end within an hour ahead).
    paired_by_time = end is not None and not (isinstance(span_id, str) and span_id)
    if end is not None:
        end_ts = end.get("ts")
        if not isinstance(end_ts, datetime):
            end_ts = u_ts
        start = end_ts - timedelta(seconds=_num(end, "duration_seconds"))
        turn_end_ts = end_ts
        ok = _bool(end, "ok", default=True)
    else:
        start = u_ts
        turn_end_ts = u_ts
        ok = True
    if start > turn_end_ts:
        start = u_ts

    warning = None
    if paired_by_time:
        warning = "llm_usage paired by time (span_id missing)"
    elif span_id is None or span_id == "":
        warning = "llm_usage without span_id"
    return (
        _AssembledTurn(
            start=start,
            end=turn_end_ts,
            active_s=float((turn_end_ts - start).total_seconds()),
            ok=ok,
            trace_id=tid,
            llm=u,
            execs=[],
            anomalies=[],
            warning=warning,
        ),
        paired_by_time,
    )


def _exec_gap(turn: _AssembledTurn, ts: datetime) -> float:
    """Signed distance of `ts` from the turn window (0 when inside)."""
    if turn.start <= ts <= turn.end:
        return 0.0
    if ts < turn.start:
        return (turn.start - ts).total_seconds()
    return (ts - turn.end).total_seconds()


def _assign_execs(
    execs: list[EventRow],
    assembled: list[_AssembledTurn],
) -> tuple[dict[int, list[EventRow]], int]:
    """Globally assign each exec event to exactly one turn (or none).

    Keys, in order (robust across both tracing shapes):
    1. span_id equality — the exec event carries the turn root's span_id
       (per-turn-root shape);
    2. time containment — exec.ts inside exactly one turn's [start, end]
       (the session-root shape: exec spans are children of the turn root, so
       the events' own span_id differs, and trace_id is shared across turns);
    3. trace_id equality — only when exactly one turn carries that trace.
    Execs that match nothing return as `n_unattached` (the caller warns).
    """
    exec_map: dict[int, list[EventRow]] = {}
    unattached = 0
    for e in execs:
        e_ts = e.get("ts")
        if not isinstance(e_ts, datetime):
            unattached += 1
            continue
        span = e.get("span_id")
        trace = e.get("trace_id")
        target: _AssembledTurn | None = None
        # 1. span_id equality — the exec's span_id matches the turn's llm
        #    span (per-turn-root shape).
        if isinstance(span, str) and span:
            target = next((t for t in assembled if (t.llm or {}).get("span_id") == span), None)
        # 2. time containment — exactly one turn window contains the exec
        if target is None:
            contained = [t for t in assembled if t.start <= e_ts <= t.end]
            if len(contained) == 1:
                target = contained[0]
        # 2b. near-miss tolerance — session-root turn windows are derived as
        #     [turn_end - duration, turn_end], which can sit a sub-second
        #     away from an exec's recorded ts (observed 00:26:22 exec vs a
        #     turn starting 00:26:22.73). Attach to the NEAREST window when
        #     the exec is within the tolerance band (ties resolve to the
        #     earlier turn); a boundary exec then lands on the turn that
        #     actually ran it instead of dropping (QA ①).
        if target is None:
            banded = [
                t
                for t in assembled
                if (t.start - _EXEC_TOLERANCE) <= e_ts <= (t.end + _EXEC_TOLERANCE)
            ]
            if banded:
                exec_ts: datetime = e_ts  # closure-safe narrowed alias
                target = min(banded, key=lambda t: _exec_gap(t, exec_ts))
        # 3. trace_id equality — only when unique across turns
        if target is None and isinstance(trace, str) and trace:
            matching = [t for t in assembled if t.trace_id == trace]
            if len(matching) == 1:
                target = matching[0]
        if target is None:
            unattached += 1
        else:
            exec_map.setdefault(id(target), []).append(e)
    return exec_map, unattached


def _collect_warnings(
    n_unpaired: int,
    assembled: list[_AssembledTurn],
    n_exec_failed: int,
) -> list[str]:
    """Data-quality warnings for the meta block: unpaired llm_usage rows and
    turns with unattached execs — surfaced so silent degradation (tokens
    zeroed, anomalies dropped) becomes visible."""
    warnings: list[str] = []
    if n_unpaired > 0:
        warnings.append(f"{n_unpaired} llm_usage row(s) paired by time (span_id missing)")
    seen: set[str] = set()
    for r in assembled:
        if r.warning is not None and r.warning not in seen:
            seen.add(r.warning)
            warnings.append(r.warning)
    # Header counts exec_failed from every event; rows only attach the ~68%
    # that carry a join key. A mismatch means some failures are invisible in
    # the waterfall — surface it instead of letting the counts diverge (QA W3).
    attached_failed = sum(1 for r in assembled for a in r.anomalies if a.startswith("exec_failed"))
    if attached_failed < n_exec_failed:
        warnings.append(
            f"{n_exec_failed - attached_failed} exec_failed event(s) not attached to a turn"
        )
    return warnings


def _project_rows(
    page_rows: list[_AssembledTurn],
    compact_ts: list[datetime],
    init_ts: list[datetime],
    offset: int,
    total: int,
) -> list[RunTimelineRow]:
    """Project assembled turns into wire rows: LLM model, exec leaves, tags.

    A turn that opens a process segment (the first turn at or after an
    initialize event — ava.turn resets there) carries a `restart` tag; a turn
    containing a compact event carries `compact@HH:MM`.
    """
    # Absolute (unpaged) indices: an initialize marker opens the segment at
    # the FIRST turn at/after it in the WHOLE assembled set. Scanning only
    # the page slice would tag every page's first row as a segment start on
    # offset > 0 (QA N2).
    segment_starts: set[int] = set()
    for ts in sorted(init_ts):
        for idx in range(offset, offset + len(page_rows)):
            if page_rows[idx - offset].start >= ts:
                segment_starts.add(idx)
                break

    return [
        RunTimelineRow(
            turn=i + offset + 1,
            start=r.start,
            end=r.end,
            active_s=r.active_s,
            ok=r.ok,
            trace_id=r.trace_id,
            llm=_llm_model(r.llm) if r.llm is not None else None,
            execs=[
                RunTimelineExec(
                    tool="execute_code",
                    dur_s=_num(e, "duration_seconds"),
                    ok=e.get("event_name") == "exec",
                )
                for e in r.execs
            ],
            anomalies=r.anomalies,
            tags=_row_tags(
                r, compact_ts, i + offset, segment_starts, is_last_row=i + offset == total - 1
            ),
        )
        for i, r in enumerate(page_rows)
    ]


def _row_tags(
    r: _AssembledTurn,
    compact_ts: list[datetime],
    abs_index: int,
    segment_starts: set[int],
    *,
    is_last_row: bool,
) -> list[str]:
    """A turn's lifecycle tags.

    A compact event inside the turn's window tags the row. A compact
    sitting at the session-route END — the default window ends AT the
    compact, which Loki's half-open range places after the last turn's end —
    tags the last row so the purple marker has a home (QA W4).
    """
    tags = [f"compact@{ts.strftime('%H:%M')}" for ts in compact_ts if r.start <= ts <= r.end]
    if not tags and is_last_row:
        tags += [
            f"compact@{ts.strftime('%H:%M')}"
            for ts in compact_ts
            if ts >= r.end and ts <= r.end + timedelta(minutes=5)
        ]
    if abs_index in segment_starts:
        tags.append("restart")
    if r.warning is not None:
        tags.append("unpaired")
    return tags


def _dedupe_markers(ts_list: list[datetime], window_s: float = 5.0) -> list[datetime]:
    """Collapse lifecycle markers that are near-duplicates of one event.

    One lifecycle action emits an audit event and a telemetry event ~1-2s
    apart (`resurrect` + `agent_resurrected`, `restart` + `restart_completed`).
    Keeping both would double-count the session boundary / restart tally, so
    markers closer than `window_s` collapse to the earlier of the pair.
    """
    if not ts_list:
        return ts_list
    ordered = sorted(ts_list)
    deduped: list[datetime] = []
    for ts in ordered:
        if deduped and (ts - deduped[-1]).total_seconds() < window_s:
            continue  # same lifecycle event, keep the earlier marker
        deduped.append(ts)
    return deduped


def _resolve_window(
    from_: datetime | None,
    to: datetime | None,
    *,
    agent_id: int,
) -> tuple[datetime, datetime, datetime | None, datetime | None]:
    """Resolve the effective window.

    Default (neither bound given) = session route: probe back for the latest
    `compact` and the latest `initialize` before it, then window
    `[initialize, compact]` (or `[initialize, now]` when the session has not
    compacted). An explicit `from`/`to` wins outright (no probing).
    """
    now = datetime.now(UTC)
    if from_ is not None or to is not None:
        lo = from_ or (to - timedelta(hours=24) if to else now - timedelta(hours=24))
        hi = to or now
        if lo >= hi:
            raise HTTPException(status_code=422, detail="from must precede to")
        return lo, hi, None, None

    probe_from = now - timedelta(hours=_SESSION_PROBE_HOURS)
    compacts_raw, _ = loki_events.query_events(
        agent_id=agent_id,
        event_names=list(_COMPACT_EVENTS),
        from_=probe_from,
        to=now,
        limit=_EVENT_PAGE,
        direction="backward",
    )
    inits_raw, _ = loki_events.query_events(
        agent_id=agent_id,
        event_names=list(_INITIALIZE_EVENTS),
        from_=probe_from,
        to=now,
        limit=_EVENT_PAGE,
        direction="backward",
    )
    compacts = [cast(EventRow, r) for r in compacts_raw]
    inits = [cast(EventRow, r) for r in inits_raw]

    compact_ts = compacts[0].get("ts") if compacts else None
    compact_at = compact_ts if isinstance(compact_ts, datetime) else None
    # Initialize = the latest initialize marker at or before the compact
    # (or the latest overall when the session never compacted).
    init_ts: list[datetime] = []
    for r in inits:
        ts = r.get("ts")
        if isinstance(ts, datetime):
            init_ts.append(ts)
    init_ts = _dedupe_markers(init_ts)
    inits_sorted = sorted(init_ts, reverse=True)
    init_at = next((ts for ts in inits_sorted if compact_at is None or ts <= compact_at), None)
    if init_at is None:
        init_at = min(inits_sorted, default=None)
    lo = init_at or (compact_at - timedelta(hours=24) if compact_at else probe_from)
    hi = compact_at or now
    if lo >= hi:
        # Degenerate route (initialize after compact) — fall back to a 24h tail.
        lo, hi = now - timedelta(hours=24), now
        init_at = None
    return lo, hi, init_at, compact_at


@router.get(
    "/api/agents/{agent_id}/run-timeline",
    response_model=RunTimelineResponse,
    dependencies=[Depends(deny_isolated_result_read)],
)
async def get_run_timeline(
    request: Request,
    agent_id: int,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_ROWS)] = _DEFAULT_ROWS,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunTimelineResponse:
    """Run→turn→call timeline for one agent.

    Default window = the complete session route from context initialization
    to the latest compact (user ruling); pass `from`/`to` (ISO-8601 with
    timezone offset) to override. Rows are turn-level, ascending; `limit` /
    `offset` page them. The call level (LLM call + execs) is embedded per
    row — no second query.
    """
    if from_ is not None and from_.tzinfo is None:
        raise HTTPException(status_code=422, detail="from must include a timezone offset")
    if to is not None and to.tzinfo is None:
        raise HTTPException(status_code=422, detail="to must include a timezone offset")

    # Sync DB read in an async handler — the async-blocking lint requires the
    # pool borrow to stay off the event loop (same pattern as the inspect
    # router's blocking helpers).
    def _agent_exists() -> bool:
        with request.app.state.db_pool.connection() as conn:
            return agent_exists(conn, agent_id)

    if not await asyncio.to_thread(_agent_exists):
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")

    try:
        lo, hi, _init_at, compact_at = await asyncio.to_thread(
            _resolve_window, from_, to, agent_id=agent_id
        )
        usage = await _fetch_events(agent_id=agent_id, event_names=["llm_usage"], from_=lo, to=hi)
        turn_ends = await _fetch_events(
            agent_id=agent_id, event_names=["turn_end"], from_=lo, to=hi
        )
        execs = await _fetch_events(
            agent_id=agent_id, event_names=list(_EXEC_EVENTS), from_=lo, to=hi
        )
        compacts = await _fetch_events(
            agent_id=agent_id, event_names=list(_COMPACT_EVENTS), from_=lo, to=hi
        )
        inits = await _fetch_events(
            agent_id=agent_id, event_names=list(_INITIALIZE_EVENTS), from_=lo, to=hi
        )
    except loki_query_budget.LokiQueryBudgetError:
        raise
    except httpx.HTTPError as exc:
        raise_backend_unavailable(exc)

    rows, boundaries, meta = _build_rows(
        usage,
        turn_ends,
        execs,
        compacts,
        inits,
        limit,
        offset,
        boundary_compact_at=compact_at,
    )
    # Loki's query_range end is exclusive, so a compact event sitting exactly
    # at the window end (the default session route ends AT the compact) is
    # missed by the in-window fetch — the probe already resolved it, carry it
    # through instead of reporting a boundary-less route.
    if boundaries.compact_at is None and compact_at is not None:
        boundaries = RunTimelineBoundaries(
            initialize_at=boundaries.initialize_at,
            compact_at=compact_at,
        )

    return RunTimelineResponse(
        agent_id=agent_id,
        window_from=lo,
        window_to=hi,
        boundaries=boundaries,
        meta=meta,
        rows=rows,
    )
