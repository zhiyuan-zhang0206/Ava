"""System broadcast SSE endpoints — /api/system, /api/agents/{agent_id}/system,
/api/system/all.

Three channels:
- /api/system forwards GLOBAL_ROLES for *all* agents (the cross-agent,
  low-frequency lifecycle the sidebar / pages popover need).
- /api/agents/{id}/system forwards the full SYSTEM_ROLES for one agent (the
  high-frequency per-turn stream the timeline of the observed agent needs).
- /api/system/all forwards EVERY event for EVERY agent in a throttled,
  batched stream (push cadence set by settings.gateway.sse_throttle_rate, each push
  a JSON array of events). No agent_id or role filtering — the frontend
  filters internally.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from gateway.sse import event_stream, throttled_event_stream
from shared.config import settings
from shared.live_events import GLOBAL_ROLES, SYSTEM_ROLES

router = APIRouter()


@router.get("/api/system")
async def get_system_broadcast(request: Request) -> StreamingResponse:
    """Global SSE broadcast — does not filter by agent_id, forwards the
    cross-agent low-frequency GLOBAL_ROLES (agent lifecycle + page open/close)
    for every agent.

    The per-turn streaming roles are NOT here — clients subscribe to
    /api/agents/{id}/system for the agent they are observing.
    """
    return StreamingResponse(
        event_stream(
            settings.data_plane.redis_url,
            0,  # agent_id is ignored in broadcast mode
            request,
            channel=settings.data_plane.events_channel,
            role_filter=GLOBAL_ROLES,
            broadcast=True,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/agents/{agent_id}/system")
async def get_system_events(agent_id: int, request: Request) -> StreamingResponse:
    """SSE endpoint — subscribe to `ava:events`, only forward SYSTEM_ROLES events.

    SYSTEM_ROLES contains code_delta / exec_output / compact_done /
    cancelled etc. "system state" events — clients that need to track the
    kernel runtime state subscribe to this channel.
    """
    return StreamingResponse(
        event_stream(
            settings.data_plane.redis_url,
            agent_id,
            request,
            channel=settings.data_plane.events_channel,
            role_filter=SYSTEM_ROLES,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/system/all")
async def get_all_events(request: Request) -> StreamingResponse:
    """Throttled all-events SSE broadcast — pushes EVERY event for EVERY agent,
    no agent_id or role filtering.  Events are batched and flushed at the
    cadence set by settings.gateway.sse_throttle_rate, each flush carrying a JSON array
    of raw event payloads.

    Wire format: `data: [event_json, ...]\n\n`

    This is the "push everything once" stream — the frontend subscribes to
    this single connection and filters internally by agent_id / role.
    """
    return StreamingResponse(
        throttled_event_stream(
            settings.data_plane.redis_url,
            request,
            channel=settings.data_plane.events_channel,
            throttle_rate=settings.gateway.sse_throttle_rate,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
