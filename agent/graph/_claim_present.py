"""Claim-node display logic: SSE event publishing for the frontend timeline.

Extracted from agent/graph/_claim.py (Phase 2 display isolation).
"""

from __future__ import annotations

from typing import Any

from agent.db import list_chat_inbound_anchors
from agent.graph._context import AvaContext
from shared.live_events import InboundCommitted, TimelineSnapshot
from shared.timeline import DEFAULT_TIMELINE_LIMIT, build_timeline_items, tail_window


async def publish_inbound_committed(ctx: AvaContext, agent_id: int, inbound_ids: list[int]) -> None:
    """Publish one InboundCommitted SSE event per chat inbound that has been
    envelope-wrapped into state.messages, triggering frontend timeline reload."""
    assert ctx.event_publisher is not None, (  # noqa: S101
        "publish_inbound_committed requires ctx.event_publisher"
    )
    for inbound_id in inbound_ids:
        ctx.event_publisher.emit(
            InboundCommitted(agent_id=agent_id, inbound_id=inbound_id).model_dump_json()
        )


async def publish_end_timeline_snapshot(
    ctx: AvaContext,
    state: Any,  # AgentState (lazy import to avoid circular)
    agent_id: int,
    new_msgs: list,
) -> None:
    """Publish a TimelineSnapshot when claim routes the agent to END.

    When claim routes the agent to END (terminate / restart), no further node
    enters, so the enter-time TimelineSnapshot (published by node_lifecycle
    before dispatch) missed the lifecycle markers appended here (e.g. the
    "You are terminated" system note). Without this final snapshot the
    frontend sees the markers only after the next manual refresh — publish
    the post-dispatch state now so the frontend renders them immediately.

    Best-effort: ctx.event_publisher / ctx.ops_pool are already narrowed to
    non-None by the container-mode early-return at the top of claim_node_impl.
    """
    assert ctx.ops_pool is not None, (  # noqa: S101
        "publish_end_timeline_snapshot requires ctx.ops_pool"
    )
    assert ctx.event_publisher is not None, (  # noqa: S101
        "publish_end_timeline_snapshot requires ctx.event_publisher"
    )
    combined = list(state.messages) + new_msgs
    anchors = await list_chat_inbound_anchors(ctx.ops_pool, agent_id)
    items, msg_count = build_timeline_items(combined, anchors)
    window, _ = tail_window(items, DEFAULT_TIMELINE_LIMIT)
    # No system-prompt special-casing: this end-of-life snapshot is a rare
    # full-window publish, and 0.0 flows through the merge like any item.
    ctx.event_publisher.emit(
        TimelineSnapshot(
            agent_id=agent_id,
            items=[it.model_dump() for it in window],
            msg_count=msg_count,
        ).model_dump_json()
    )
