"""Claim-node display logic: SSE event publishing for the frontend timeline.

Extracted from agent/graph/claim/node.py (Phase 2 display isolation).
"""

from __future__ import annotations

from typing import Any

from base.agents.context import MeivaContext
from base.agents.history.timeline import build_timeline_items, tail_window, timeline_default_limit
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.events.live.projection import InboundCommitted, TimelineSnapshot


async def publish_inbound_committed(
    ctx: MeivaContext, agent_id: int, inbound_ids: list[int]
) -> None:
    """Publish one InboundCommitted SSE event per chat inbound that has been
    envelope-wrapped into state.messages, triggering frontend timeline reload."""
    assert ctx.event_publisher is not None, (  # noqa: S101
        "publish_inbound_committed requires ctx.event_publisher"
    )
    for inbound_id in inbound_ids:
        ctx.event_publisher.emit(
            InboundCommitted(agent_id=agent_id, inbound_id=inbound_id).model_dump_json()
        )


def publish_end_timeline_snapshot(
    ctx: MeivaContext,
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

    ctx.event_publisher is already narrowed to non-None by the container-mode
    early-return at the top of claim_node_impl.
    """
    assert ctx.event_publisher is not None, (  # noqa: S101
        "publish_end_timeline_snapshot requires ctx.event_publisher"
    )
    combined = list(state.messages) + new_msgs
    items, msg_count = build_timeline_items(
        combined,
        inputs=TimelineReadInputs(
            ctx.require_clock, lambda: ctx.require_agent().read("general", "message_timestamps")
        ),
    )
    window, _ = tail_window(
        items,
        timeline_default_limit(
            limit_reader=lambda: ctx.require_agent().read("display", "timeline_default_limit")
        ),
    )
    # No system-prompt special-casing: this end-of-life snapshot is a rare
    # full-window publish, and 0.0 flows through the merge like any item.
    ctx.event_publisher.emit(
        TimelineSnapshot(
            agent_id=agent_id,
            items=[it.model_dump() for it in window],
            msg_count=msg_count,
        ).model_dump_json()
    )
