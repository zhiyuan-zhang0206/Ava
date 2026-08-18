"""Agent events endpoints — /api/agents/{id}/events*.

Two faces of the same observability stream:
- `GET …/events/stream`: the live SSE tail (subscribe to Redis `ava:events`,
  filter by `agent_id`) — only carries events from the moment you subscribe.
- `GET …/events`: the historical REST query over Loki (the LGTM read side
  of the unified emitter; task #1197) — the past, for ops / SDK / eval
  consumers. Was a PG `events` table read before the LGTM cutover.

Split out of routers/agents.py (CRUD + lifecycle then; lifecycle later moved
to routers/agents_lifecycle.py) so each router stays a
focused unit — same precedent as routers/agent_inspect.py (the per-agent
inspector panel).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from gateway import loki_events
from gateway.schemas import AgentEventRow
from gateway.sse import event_stream
from shared.config import settings

router = APIRouter()


@router.get("/api/agents/{agent_id}/events/stream")
async def get_events_stream(agent_id: int, request: Request) -> StreamingResponse:
    """SSE endpoint — subscribe to Redis `ava:events`, filter by `agent_id`,
    forward.

    Client `EventSource` receives `data: {json}\\n\\n` frames, one JSON
    `events.Event` per frame. code_delta chunks pass through immediately,
    native streaming. This is the live tail; `GET /api/agents/{id}/events`
    (no `/stream`) is the historical REST query over the persisted
    `events` table.

    Does **not** check agent_exists as a precondition: subscribing to a
    non-existent agent is allowed, you just receive no messages. Otherwise
    agents that were just switched / newly created but with slight DB
    visibility lag would 404.
    """
    return StreamingResponse(
        event_stream(settings.data_plane.redis_url, agent_id, request),
        media_type="text/event-stream",
        headers={
            # Reverse proxies like nginx / cloudflare buffer text responses
            # by default — these two headers tell them to pass bytes through.
            # Not needed when running uvicorn directly, but without them
            # everything breaks behind a proxy; cheap to write so we do it.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/agents/{agent_id}/events")
def get_agent_events(
    agent_id: int,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    event: Annotated[str | None, Query()] = None,
    level: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AgentEventRow]:
    """Historical slice of this agent's `events` rows — the REST
    counterpart to the live `…/events/stream` SSE tail (which only carries
    events from the moment you subscribe). Backs ops / SDK / eval consumers
    that need to query the past.

    Filters compose (AND):
      - `from=<ISO-8601>` / `to=<ISO-8601>`: inclusive time window
        (`ts >= from AND ts <= to`); `from` omitted defaults to the last
        24h (same lower-bound contract as /api/events). Bad timestamps 422
        (FastAPI datetime parse).
      - `event=<name>`: exact event type, e.g. `llm_usage` / `turn_end` /
        `exec` / `exec_failed`.
      - `level=<INFO|WARNING|ERROR|…>`: exact level (case-insensitive, any
        case accepted; the stored/returned values are LOWERCASE since the W9
        events-table switch — `agent_events`' uppercase `INFO`/`WARNING`/
        `ERROR` no longer appears on the wire). This is an *exact* match,
        unlike `/api/cluster/admin/events` where `level`
        is a minimum threshold — a per-agent history query reads more
        predictably as "give me exactly this level".

    Returns newest-first (`ts DESC`; `id` is a stable surrogate derived
    from the log line, so `limit`/`offset` paging stays deterministic).
    `limit` defaults to 100, capped at 1000 (over-limit 422s); `offset`
    pages further back (Loki has no native offset — the page is sliced in
    memory, so deep offsets cost a larger fetch).

    No agent-existence precondition (same as `…/activity` and `…/pending`):
    an unknown agent or an empty window just returns `[]`.
    """
    rows, _ = loki_events.query_events(
        agent_id=agent_id,
        categories=["telemetry", "log"],
        event_names=[event] if event is not None else None,
        level=level,
        from_=from_,
        to=to,
        limit=limit,
        offset=offset,
    )
    return [
        AgentEventRow(
            id=row["id"],
            ts=row["ts"],
            agent_id=row["agent_id"],
            level=row["level"],
            event=row["event_name"],
            payload=row["attributes"],
        )
        for row in rows
    ]
