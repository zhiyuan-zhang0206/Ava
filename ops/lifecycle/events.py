"""Event-publish RPC ops — the InboundArrived / PageClosed / Notice event fan-out.

The `ops.lifecycle` door re-exports these publishes. Each helper is best-effort: the publish goes to
the shared events channel (`EventBus.publish_best_effort`) so a Redis blip costs a live
UI update, never the durable op that triggered it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TypedDict

from base.events.live.bus import EventBus
from base.events.live.projection import (
    InboundArrived,
    NoticePosted,
    NoticeResolved,
    PageClosed,
)


async def publish_inbound_arrived(
    bus: EventBus, agent_id: int, inbound_id: int, kind: str, source: str, content: str
) -> None:
    """Publish InboundArrived to the events channel — frontend live updates."""
    ev = InboundArrived(
        agent_id=agent_id,
        inbound_id=inbound_id,
        kind=kind,
        source=source,
        content=content,
    )
    await bus.publish_best_effort(ev.model_dump_json(), context="inbound_arrived")


async def publish_page_closed(bus: EventBus, agent_id: int, name: str) -> None:
    """Publish PageClosed to the events channel — frontend popover removes the entry.
    Best-effort, never raises."""
    ev = PageClosed(agent_id=agent_id, name=name)
    await bus.publish_best_effort(ev.model_dump_json(), context="page_closed")


class NoticeResolutionFacts(TypedDict):
    resolution: str
    require_response: bool
    age_seconds: int
    had_reply: bool


def notice_resolution_facts(
    resolution: str,
    *,
    require_response: bool,
    created_at: datetime,
    replied: bool,
) -> NoticeResolutionFacts:
    return NoticeResolutionFacts(
        resolution=resolution,
        require_response=require_response,
        age_seconds=max(0, int((datetime.now(UTC) - created_at).total_seconds())),
        had_reply=replied,
    )


async def publish_notice_resolved(
    bus: EventBus,
    agent_id: int,
    notice_id: int,
    *,
    facts: NoticeResolutionFacts | None = None,
    superseded_by: int | None = None,
) -> None:
    """Publish NoticeResolved to refresh the open and resolved Inbox views.
    Best-effort, never raises."""
    ev = NoticeResolved(
        agent_id=agent_id,
        notice_id=notice_id,
        resolution=None if facts is None else facts["resolution"],
        require_response=None if facts is None else facts["require_response"],
        age_seconds=None if facts is None else facts["age_seconds"],
        had_reply=None if facts is None else facts["had_reply"],
        superseded_by=superseded_by,
    )
    await bus.publish_best_effort(ev.model_dump_json(), context="notice_resolved")


async def publish_notice_posted(
    bus: EventBus,
    agent_id: int,
    notice_id: int,
    priority: str,
    title: str,
    task_id: int | None = None,
) -> None:
    """Publish NoticePosted so the frontend refreshes the open Inbox queue.
    Best-effort, never raises. `task_id` groups the feed by task without a
    refetch."""
    ev = NoticePosted(
        agent_id=agent_id, notice_id=notice_id, priority=priority, title=title, task_id=task_id
    )
    await bus.publish_best_effort(ev.model_dump_json(), context="notice_posted")
