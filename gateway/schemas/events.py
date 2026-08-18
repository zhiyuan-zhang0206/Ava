"""agent event rows.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from datetime import datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
)


class AgentEventRow(BaseModel):
    """One row from the `events` PG table — admin event log entry (category=telemetry/log).

    `agent_id` is None for service-level lines (gateway / scheduler / labeler
    / runner / etc.) and an int for agent process lines. `payload` keeps the
    full structured record (msg, kwargs from logger.bind / kwargs from the
    call, plus traceback fields when an exception was attached).
    `level` is LOWERCASE (`debug`/`info`/`warning`/`error`/`critical`): the
    W9 switch from the legacy `agent_events` table (loguru's uppercase `INFO`/`WARNING`/
    `ERROR`) flipped the wire values — consumers matching uppercase must
    adapt."""

    model_config = ConfigDict(frozen=True)

    id: int
    ts: datetime
    agent_id: int | None
    level: str
    event: str
    payload: dict[str, Any]


class AgentEventsResponse(BaseModel):
    """GET /api/cluster/admin/events response — newest-first slice."""

    model_config = ConfigDict(frozen=True)

    items: list[AgentEventRow]


class EventRow(BaseModel):
    """One row from the unified `events` table — the single event stream.

    Every signal shares this shape (event-system design doc §1): audit
    (legacy `event_log`), telemetry and log (formerly `agent_events`) all live
    here, written through the unified emitter (`shared/telemetry.py`).
    `trace_id` is the correlation key — one turn = one trace id, every event
    inside it carries the same value. `agent_id` is None for service-level
    events (gateway / daemons); `machine` is the host dimension. `level` is
    LOWERCASE (`debug`/`info`/`warning`/`error`/`critical`) — the retired
    `agent_events` mirror stored loguru's uppercase; the W9 read-path switch
    flipped API consumers to lowercase."""

    model_config = ConfigDict(frozen=True)

    id: int
    ts: datetime
    trace_id: str | None
    span_id: str | None
    agent_id: int | None
    machine: str
    process: str
    category: str
    event_name: str
    level: str
    source: str
    target_agent_id: int | None
    attributes: dict[str, Any]


class EventsMeta(BaseModel):
    """GET /api/events response header — effective window + pagination
    state. `window_from`/`window_to` echo the applied time window
    (`None` when unbounded high; `window_from` is never `None` — the
    computed `now - hours` when the request used `hours`, the explicit
    `from` when one was given, and otherwise the default lower bound
    `now - 24h`). `has_more` (from the list fetch's +1 lookahead) tells a
    paging client whether another `offset` page exists. `total` is the exact
    filtered row count before paging, computed only when the request asked
    for it (`with_total=1`) — `None` otherwise (it costs a full-window count
    aggregation)."""

    model_config = ConfigDict(frozen=True)

    total: int | None
    window_from: datetime | None
    window_to: datetime | None
    limit: int
    offset: int
    has_more: bool
    generated_at: str  # ISO-8601 UTC


class EventsResponse(BaseModel):
    """GET /api/events response — newest-first slice of the unified event
    stream (`ts DESC`, `id DESC` tiebreak so `limit`/`offset` paging is stable
    across same-`ts` rows)."""

    model_config = ConfigDict(frozen=True)

    meta: EventsMeta
    items: list[EventRow]
