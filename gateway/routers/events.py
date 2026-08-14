"""Unified event stream query — `GET /api/events`.

The programmatic query surface over the unified event stream (audit /
telemetry / log — the LGTM read side, task #1197: the PG `events` read was
replaced by Loki). One schema and one correlation key (`trace_id`), served
from `gateway/loki_events.py`; wire shape and filter semantics are unchanged
from the PG version.

Filters compose (AND): `category` / `event_name` (`kind` kept as a
legacy alias) / `agent_id` / `trace_id` / `machine` / `level`, plus a
time window given either as `from`/`to`
(ISO-8601, inclusive) or as `hours` (the last N hours — shorthand for
`from = now - hours`; the two forms are mutually exclusive). `level` is an
exact match (case-insensitive), the same reading as the per-agent events
query. Pagination is `limit` + `offset`; `meta.total` is the filtered row
count before paging and `meta.has_more` tells the client whether another
page exists.

Two hard contract rules keep every query bounded and unambiguous:
  - a lower bound is always in effect — absent both `from` and `hours`,
    `from = now - 24h` is assumed (the old PG scan needed the partition
    prune; on Loki it keeps the count/list fetch cheap — the API never
    runs an unbounded window);
  - `from` / `to` must carry a timezone offset — a naive timestamp would be
    interpreted in the server's local timezone, silently shifting the
    window; such input is rejected with 422 instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from gateway import loki_events
from gateway.schemas import EventRow, EventsMeta, EventsResponse

router = APIRouter()

# The events table stores these lowercase (design doc §1); unknown values are
# rejected with 422 rather than silently matching nothing (fail fast).
_CATEGORIES = frozenset({"audit", "telemetry", "log"})
_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})

# Longest retention (audit = 365d); anything longer is a no-op window anyway.
_MAX_HOURS = 24 * 365

# Default window when the request names no lower bound (`from`/`hours`) —
# same contract as the old PG API (which pruned to the current month
# partitions); on Loki it bounds the count/list fetch.
_DEFAULT_WINDOW_HOURS = 24


def _validate(
    *,
    category: str | None,
    level: str | None,
    from_: datetime | None,
    to: datetime | None,
    hours: float | None,
) -> str | None:
    """Validate enum-ish filters; return the normalized `level` (lowercase)
    or raise 422. `from` + `hours` together is a contradiction — reject it
    instead of silently picking one. Naive timestamps are rejected too: a
    tz-less `from`/`to` would be interpreted in the server's local timezone,
    silently shifting the window — fail fast instead of guessing."""
    if category is not None and category not in _CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"category must be one of {sorted(_CATEGORIES)}, got {category!r}",
        )
    if level is not None:
        level = level.lower()
        if level not in _LEVELS:
            raise HTTPException(
                status_code=422,
                detail=f"level must be one of {sorted(_LEVELS)}, got {level!r}",
            )
    for name, value in (("from", from_), ("to", to)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{name} must include a timezone offset "
                    f"(e.g. 2026-08-04T00:00:00Z) — naive timestamps are "
                    f"interpreted in the server's local timezone"
                ),
            )
    if from_ is not None and hours is not None:
        raise HTTPException(
            status_code=422,
            detail="from and hours are mutually exclusive — pass one time window",
        )
    return level


@router.get("/api/events")
def get_events(
    category: Annotated[str | None, Query()] = None,
    event_name: Annotated[str | None, Query()] = None,
    # Legacy alias for `event_name` (pre term-alignment clients). Retired
    # 2026-09-30 — see the docstring; remove alias + conflict logic then.
    kind: Annotated[str | None, Query()] = None,
    agent_id: Annotated[int | None, Query()] = None,
    trace_id: Annotated[str | None, Query()] = None,
    machine: Annotated[str | None, Query()] = None,
    level: Annotated[str | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    hours: Annotated[float | None, Query(gt=0, le=_MAX_HOURS)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventsResponse:
    """Slice of the unified event stream — every event (audit / telemetry /
    log) through one query surface, the programmatic read side of the
    unified emitter. Newest-first (`ts DESC`, `id DESC` tiebreak).

    Filters compose (AND):
      - `category=<audit|telemetry|log>`: retention/alerting class (unknown
        value 422s).
      - `event_name=<name>`: exact event name, e.g. `llm_usage` /
        `turn_end` / `spawn` / `send_message` (the legacy `kind=` spelling
        is accepted as an alias; passing both with different values 422s).
        The `kind=` alias is retired on 2026-09-30 — pre-alignment clients
        must move to `event_name` before then, after which the alias and its
        conflict check are removed.
      - `agent_id=<n>`: events belonging to that agent; service-level events
        (NULL) are excluded when set.
      - `trace_id=<hex>`: one turn's whole call chain — every event that
        shares the id.
      - `machine=<name>`: the host dimension.
      - `level=<debug|info|warning|error|critical>`: exact match,
        case-insensitive (unknown value 422s).
      - `from=<ISO-8601>` / `to=<ISO-8601>`: inclusive time window
        (`ts >= from AND ts <= to`); either side may be omitted. Values
        MUST carry a timezone offset (`Z` or `+hh:mm`) — a naive timestamp
        is interpreted in the server's local timezone, so it 422s instead
        of silently shifting the window.
      - `hours=<n>`: alternative window — the last N hours
        (`from = now - hours`). Mutually exclusive with `from`.
      - Default window: when neither `from` nor `hours` is given, the last
        24 hours are assumed (`from = now - 24h`) — an unbounded query
        would scan the whole retention history (6M+ rows across every
        month partition), so the API never runs one. `meta.window_from`
        always echoes the effective lower bound.
      - `limit` (default 100, cap 1000) / `offset`: offset paging; stable
        ordering across same-`ts` rows.

    Response: `meta` (exact filtered `total` from the Loki count path,
    effective `window_from`/`window_to`, `limit`/`offset`, `has_more`) +
    `items` (the unified `EventRow` shape). `window_from` is always set —
    the default 24h lower bound when the request named none. An empty
    window returns `total: 0` and `items: []`.
    """
    level = _validate(category=category, level=level, from_=from_, to=to, hours=hours)

    now = datetime.now(UTC)
    window_from = from_
    if hours is not None:
        window_from = now - timedelta(hours=hours)
    if window_from is None:
        # No explicit lower bound — default to the last 24h (the same
        # lower-bound contract as the old PG API; A31).
        window_from = now - timedelta(hours=_DEFAULT_WINDOW_HOURS)

    if event_name is not None and kind is not None and event_name != kind:
        raise HTTPException(
            status_code=422,
            detail=(
                "event_name and its legacy alias kind disagree — pass one "
                f"(event_name={event_name!r}, kind={kind!r})"
            ),
        )
    name = event_name if event_name is not None else kind

    total = loki_events.count_events(
        agent_id=agent_id,
        categories=[category] if category is not None else None,
        event_names=[name] if name is not None else None,
        trace_id=trace_id.lower() if trace_id is not None else None,
        machine=machine,
        level=level,
        from_=window_from,
        to=to,
    )
    rows, _ = loki_events.query_events(
        agent_id=agent_id,
        categories=[category] if category is not None else None,
        event_names=[name] if name is not None else None,
        trace_id=trace_id.lower() if trace_id is not None else None,
        machine=machine,
        level=level,
        from_=window_from,
        to=to,
        limit=limit,
        offset=offset,
    )

    items = [
        EventRow(
            id=row["id"],
            ts=row["ts"],
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            agent_id=row["agent_id"],
            machine=row["machine"],
            process=row["process"],
            category=row["category"],
            event_name=row["event_name"],
            level=row["level"],
            source=row["source"],
            target_agent_id=row["target_agent_id"],
            attributes=row["attributes"],
        )
        for row in rows
    ]
    meta = EventsMeta(
        total=total,
        window_from=window_from,
        window_to=to,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
        generated_at=now.isoformat(),
    )
    return EventsResponse(meta=meta, items=items)
