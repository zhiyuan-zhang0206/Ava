"""Unified event stream query — `GET /api/events`.

The programmatic query surface over the unified event stream (audit /
telemetry / log). One schema and one correlation key (`trace_id`). Audit rows
are read from `audit_events` (`base/events/reads/audit_rows.py`) and telemetry and
log rows from `telemetry_events` (`gateway/events/telemetry_rows.py`), both in
Postgres and both permanent. A request that spans both is answered by one
merge, newest first. `telemetry_events` stores only the events a reader queries by name and
every event at warning level or above, so a trace's full chain is in Loki (84 hours) or the
JSONL event mirror.

Filters compose (AND): `category` / `event_name` / `tier` / `agent_id` /
`trace_id` / `machine` / `level`, plus a
time window given either as `from`/`to`
(ISO-8601, inclusive) or as `hours` (the last N hours — shorthand for
`from = now - hours`; the two forms are mutually exclusive). `level` is an
exact match (case-insensitive), the same reading as the per-agent events
query. Pagination is `limit` + `offset`; `meta.has_more` (from the list
fetch's +1 lookahead) tells the client whether another page exists, and
`meta.total` — the exact filtered row count before paging — is opt-in via
`with_total=1` (it costs a full-window count aggregation, which a page flip
does not need).

Two hard contract rules keep every query bounded and unambiguous:
  - a lower bound is always in effect — absent both `from` and `hours`,
    `from = now - 24h` is assumed (the old PG scan needed the partition
    prune; it keeps the count/list fetch cheap — the API never
    runs an unbounded window);
  - `from` / `to` must carry a timezone offset — a naive timestamp would be
    interpreted in the server's local timezone, silently shifting the
    window; such input is rejected with 422 instead.
"""

from __future__ import annotations

import re
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from base.config import settings
from base.events.contract import EVENTS, EventTier, tier_for
from base.events.reads import audit_rows
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.events import telemetry_rows
from gateway.events.schemas import EventRow, EventsMeta, EventsResponse

router = APIRouter()

# These are stored lowercase (design doc §1); unknown values are
# rejected with 422 rather than silently matching nothing (fail fast).
_CATEGORIES = frozenset({"audit", "telemetry", "log"})
_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})
_TIERS = ("business", "anomaly", "observation", "noise")
_IMPERSONATION_SESSION = re.compile(r"^[0-9]+:[0-9]+$")

# Longest window a request may name; a protective constant, evaluated at import for the `hours` Query
# bound — not configuration (task #3696 exception inventory: KEEP).
_MAX_HOURS = 24 * 365

# Default window when the request names no lower bound (`from`/`hours`);
# it bounds the count/list fetch.
_DEFAULT_WINDOW_HOURS = 24

# Bound on one read, below the route's client timeouts.
_READ_STATEMENT_TIMEOUT_MS = 10_000


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


def _parse_tiers(tier: str | None) -> list[EventTier] | None:
    """Normalize a comma-separated tier filter or fail fast with 422."""
    if tier is None:
        return None
    tiers: list[EventTier] = []
    for raw in tier.split(","):
        value = raw.strip().lower()
        if value not in _TIERS:
            raise HTTPException(
                status_code=422,
                detail=f"tier must be a comma-separated list of {_TIERS}, got {tier!r}",
            )
        if value not in tiers:
            tiers.append(value)
    return tiers


@dataclass(frozen=True)
class _Filters:
    """The filters every store applies, already validated and normalized."""

    agent_id: int | None
    event_names: list[str] | None
    tiers: list[EventTier] | None
    trace_id: str | None
    machine: str | None
    level: str | None
    attribute_filters: dict[str, str] | None
    from_: datetime | None
    to: datetime | None


@contextmanager
def _read_connection(request: Request) -> Generator[psycopg.Connection]:
    """One pooled connection for an events read, bounded by a statement timeout."""
    with request.app.state.db_pool.connection() as conn:
        conn.execute(f"SET LOCAL statement_timeout = {_READ_STATEMENT_TIMEOUT_MS}")
        yield conn


def _sources(category: str | None, event_name: str | None) -> tuple[bool, bool]:
    """Which tables a request reads: `(audit_events, telemetry_events)`.

    An explicit category picks one table. Without one, a registered event name
    picks the table(s) its declared categories live in; anything else reads both.
    """
    if category is not None:
        return category == "audit", category != "audit"
    spec = EVENTS.get(event_name) if event_name is not None else None
    if spec is None:
        return True, True
    categories = {spec.category, *spec.extra_categories}
    return "audit" in categories, bool(categories - {"audit"})


def _impersonation_filters(session: str | None) -> dict[str, str] | None:
    """Validate the private replay correlation value and build its Loki filter."""
    if session is None:
        return None
    if not _IMPERSONATION_SESSION.fullmatch(session):
        raise HTTPException(
            status_code=422,
            detail="impersonation_session must be an agent_id:session_id pair",
        )
    return {"impersonation_session": session}


def _count(
    request: Request,
    filters: _Filters,
    use_audit: bool,  # noqa: FBT001 — internal helper flags, always positional
    use_telemetry: bool,  # noqa: FBT001
    telemetry_categories: list[str],
) -> int:
    """Exact filtered row count across the tables the request reads."""
    total = 0
    with _read_connection(request) as conn:
        if use_audit:
            total += audit_rows.count_events(conn, **asdict(filters))
        if use_telemetry:
            total += telemetry_rows.count_events(
                conn, categories=telemetry_categories, **asdict(filters)
            )
    return total


def _read_page(
    request: Request,
    filters: _Filters,
    use_audit: bool,  # noqa: FBT001 — internal helper flags, always positional
    use_telemetry: bool,  # noqa: FBT001
    telemetry_categories: list[str],
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], bool]:
    """One newest-first page, and whether another exists.

    One table pages directly. Two tables each give their newest `offset + limit`
    rows, which are merged and then sliced: a global page never needs a row
    beyond that depth from either side.
    """
    both = use_audit and use_telemetry
    depth, page_offset = (limit + offset, 0) if both else (limit, offset)
    rows: list[dict[str, Any]] = []
    has_more = False
    with _read_connection(request) as conn:
        if use_audit:
            page, more = audit_rows.query_events(
                conn, limit=depth, offset=page_offset, **asdict(filters)
            )
            rows += page
            has_more = has_more or more
        if use_telemetry:
            page, more = telemetry_rows.query_events(
                conn,
                categories=telemetry_categories,
                limit=depth,
                offset=page_offset,
                **asdict(filters),
            )
            rows += page
            has_more = has_more or more
    if both:
        rows.sort(key=lambda row: (row["ts"], row["id"]), reverse=True)
        has_more = has_more or len(rows) > limit + offset
        rows = rows[offset : offset + limit]
    return rows, has_more


@router.get("/api/events", dependencies=[Depends(deny_isolated_result_read)])
def get_events(
    request: Request,
    category: Annotated[str | None, Query()] = None,
    event_name: Annotated[str | None, Query()] = None,
    agent_id: Annotated[int | None, Query()] = None,
    trace_id: Annotated[str | None, Query()] = None,
    impersonation_session: Annotated[str | None, Query()] = None,
    machine: Annotated[str | None, Query()] = None,
    level: Annotated[str | None, Query()] = None,
    tier: Annotated[str | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    hours: Annotated[float | None, Query(gt=0, le=_MAX_HOURS)] = None,
    # `limit`'s range and `offset`'s ceiling stay protective constants (import-
    # time Query bounds); the default *window* is display.events_default_limit.
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    with_total: Annotated[bool, Query()] = False,  # noqa: FBT002 — FastAPI query param
) -> EventsResponse:
    """Slice of the unified event stream — every event (audit / telemetry /
    log) through one query surface, the programmatic read side of the
    unified emitter. Newest-first (`ts DESC`, `id DESC` tiebreak).

    Filters compose (AND):
      - `category=<audit|telemetry|log>`: retention/alerting class (unknown
        value 422s).
      - `event_name=<name>`: exact event name, e.g. `llm_usage` /
        `turn_end` / `spawn` / `send_message`.
      - `agent_id=<n>`: events belonging to that agent; service-level events
        (NULL) are excluded when set.
      - `trace_id=<hex>`: the events of one turn that share the id and that
        Postgres stores. `telemetry_events` keeps only the events a reader queries
        by name (`persist=True` in the registry) and every event at warning level or
        above, so this is not the whole chain: read the rest from Loki (84-hour
        window) or the JSONL event mirror.
      - `machine=<name>`: the host dimension.
      - `level=<debug|info|warning|error|critical>`: exact match,
        case-insensitive (unknown value 422s).
      - `tier=<business|anomaly|observation|noise>[,...]`: comma-separated
        display tiers, ORed within the list and ANDed with every other filter.
        The tier predicate is applied before pagination so `meta.total` and
        page boundaries stay exact.
      - `from=<ISO-8601>` / `to=<ISO-8601>`: inclusive time window
        (`ts >= from AND ts <= to`); either side may be omitted. Values
        MUST carry a timezone offset (`Z` or `+hh:mm`) — a naive timestamp
        is interpreted in the server's local timezone, so it 422s instead
        of silently shifting the window.
      - `hours=<n>`: alternative window — the last N hours
        (`from = now - hours`). Mutually exclusive with `from`.
      - Default window: when neither `from` nor `hours` is given, the last
        24 hours are assumed (`from = now - 24h`) — an unbounded query
        would scan the whole record, so the API never runs one. `meta.window_from`
        always echoes the effective lower bound.
      - `limit` (configured default window — ``display.events_default_limit``,
        100 out of the box — cap 1000) / `offset` (cap 10,000): offset
        paging with stable ordering across same-`ts` rows. The cap bounds the
        rows one read materializes (`limit + offset + 1`).
      - `with_total=1`: also compute the exact filtered row count
        (`meta.total`) — one extra full-window count, so it is opt-in; without it `meta.total` is null.

    Response: `meta` (opt-in exact filtered `total`, effective
    `window_from`/`window_to`, `limit`/`offset`, `has_more` from the list
    fetch's +1 lookahead) + `items` (the unified `EventRow` shape).
    `window_from` is always set — the default 24h lower bound when the
    request named none. An empty window returns `items: []`.
    """
    level = _validate(category=category, level=level, from_=from_, to=to, hours=hours)
    attribute_filters = _impersonation_filters(impersonation_session)
    tiers = _parse_tiers(tier)
    effective_limit = limit if limit is not None else settings.display.events_default_limit

    now = datetime.now(UTC)
    window_from = from_
    if hours is not None:
        window_from = now - timedelta(hours=hours)
    if window_from is None:
        # No explicit lower bound — default to the last 24h (the same
        # lower-bound contract as the old PG API; A31).
        window_from = now - timedelta(hours=_DEFAULT_WINDOW_HOURS)

    filters = _Filters(
        agent_id=agent_id,
        event_names=[event_name] if event_name is not None else None,
        tiers=tiers,
        trace_id=trace_id.lower() if trace_id is not None else None,
        machine=machine,
        level=level,
        attribute_filters=attribute_filters,
        from_=window_from,
        to=to,
    )
    use_audit, use_telemetry = _sources(category, event_name)
    telemetry_categories = [category] if category is not None else ["telemetry", "log"]

    try:
        total = (
            _count(request, filters, use_audit, use_telemetry, telemetry_categories)
            if with_total
            else None
        )
        rows, has_more = _read_page(
            request,
            filters,
            use_audit,
            use_telemetry,
            telemetry_categories,
            effective_limit,
            offset,
        )
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="events read timed out") from exc

    items = [
        EventRow(
            id=row["id"],
            line_sha256=row["line_sha256"],
            ts=row["ts"],
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            agent_id=row["agent_id"],
            machine=row["machine"],
            process=row["process"],
            category=row["category"],
            event_name=row["event_name"],
            tier=tier_for(row["event_name"], row["category"], row["level"]),
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
        limit=effective_limit,
        offset=offset,
        has_more=has_more,
        generated_at=now.isoformat(),
    )
    return EventsResponse(meta=meta, items=items)
