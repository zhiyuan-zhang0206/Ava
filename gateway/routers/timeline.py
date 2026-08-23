"""Timeline endpoint — /api/agents/{agent_id}/timeline.

Cold-load path only (page mount / agent switch). During a turn the frontend
updates from agent-published `timeline_snapshot` events; this endpoint just
serves the initial full view. Both render through the same
`shared.timeline.build_timeline_items`, so the cold load and the live
snapshots agree item-for-item.

This is a cold-load checkpoint reader (see `shared.checkpoint` for the shared
read contract). It tolerates a checkpoint read failure by rendering an empty
view + 200 so the UI is not blocked on a transient store hiccup.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from gateway.routers._eval_guard import deny_isolated_result_read
from shared.checkpoint import CheckpointReadError, load_checkpoint_messages
from shared.db import agent_exists, list_inbound_messages
from shared.timeline import (
    DEFAULT_TIMELINE_LIMIT,
    TimelineItem,
    build_timeline_items,
    tail_window,
)

router = APIRouter()
_log = logging.getLogger(__name__)


class TimelineResponse(BaseModel):
    """Cold-load timeline payload: one window of rendered items + the
    authoritative `msg_count` (len(state.messages)) + `has_more`.

    `msg_count` is surfaced (not inferred frontend-side from max rendered
    msg_idx) so the merge that preserves the single streaming "future
    partial" uses the exact boundary — a trailing message that renders to
    nothing would otherwise make an inferred count too low.

    The endpoint returns only a tail window (newest `limit` items); `before`
    pages further back for scroll-up history. `has_more` reports whether
    older items exist before the returned window.
    """

    items: list[TimelineItem]
    msg_count: int
    has_more: bool


def _window_before(
    items: list[TimelineItem], before: str, limit: int
) -> tuple[list[TimelineItem], bool]:
    """Return up to `limit` items immediately older than the `before` item_id.

    `before` is the oldest item_id the frontend currently holds. If it is
    not found (rolled off / never existed), return an empty window with no
    more — the frontend stops asking rather than looping.
    """
    idx = next((i for i, it in enumerate(items) if it.item_id == before), None)
    if idx is None:
        return [], False
    start = max(0, idx - limit) if limit > 0 else 0
    return items[start:idx], start > 0


def _item_sort_key(item_id: str) -> tuple[int, int]:
    """Order items by their logical append position (msg_idx, block_idx) parsed
    from item_id — numeric, so "10.0" follows "2.0". This is the order
    build_timeline_items already emits in; sorting by created_at would reorder
    on a clock skew now that items carry real wall-clock timestamps."""
    msg_idx, block_idx = item_id.split(".")
    return (int(msg_idx), int(block_idx))


@router.get("/api/agents/{agent_id}/timeline", dependencies=[Depends(deny_isolated_result_read)])
def get_timeline(
    agent_id: int,
    request: Request,
    limit: int = Query(default=DEFAULT_TIMELINE_LIMIT, ge=1, le=1000),
    before: str | None = Query(default=None),
) -> TimelineResponse:
    """Timeline = raw view of LangGraph state.messages, one window at a time.

    Default (no `before`) returns the newest `limit` items. Pass
    `before=<oldest item_id you hold>` to fetch the previous window for
    scroll-up history loading. `has_more` reports whether older items exist
    before the returned window.

    The full timeline is always built (one checkpoint blob); windowing only
    trims the payload + the frontend render. A checkpoint read failure renders
    an empty view + 200 (cold-load tolerance, see `shared.checkpoint`).
    """
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        # All chat inbound anchors, no limit — anchors drive ts alignment.
        chat_anchors = [
            row
            for row in list_inbound_messages(conn, agent_id, limit=100_000)
            if row.kind == "chat"
        ]
    try:
        messages = load_checkpoint_messages(agent_id)
    except CheckpointReadError as exc:
        _log.warning("timeline cold load: checkpoint read failed for agent %s: %r", agent_id, exc)
        messages = []
    items, msg_count = build_timeline_items(messages, chat_anchors)
    items.sort(key=lambda it: _item_sort_key(it.item_id))
    if before is None:
        window, has_more = tail_window(items, limit)
        # The system-prompt item (0.0) is the OLDEST item and falls off the
        # tail window for any conversation with more than `limit` rendered
        # items — windowing, not snapshot policy, is why GET must always
        # re-attach it at the window head: the expandable prompt card must
        # survive a cold load of a long conversation regardless of what SSE
        # snapshots carried. Not counted against `limit` (the window stays
        # `limit` items + the prompt), and `has_more` still reflects the
        # truncated history. A short conversation whose window already
        # contains 0.0 is untouched.
        if not any(it.item_id == "0.0" for it in window):
            prompt = next((it for it in items if it.kind == "system_prompt"), None)
            if prompt is not None:
                window = [prompt, *window]
                # `has_more` must not count the re-attached prompt: a
                # conversation whose whole history is exactly 0.0 + `limit`
                # items has nothing older, but tail_window's `len(items) >
                # limit` counted the prompt and reported has_more=True — a
                # phantom scroll-up affordance that dies on the first (empty)
                # request. Recompute against the non-prompt history.
                has_more = has_more and len(items) - 1 > limit
        # Compact summaries are standing context, same as the system prompt:
        # in a long conversation the earliest inbound_compact_summary falls
        # off the tail window, and once 0.0 is re-attached the older-paging
        # cursor (before=0.0) can never reach it — the user scrolls up to the
        # head and sees only the prompt (user report 2026-08-06). Re-attach
        # every compact_summary right after the prompt so the compressed
        # history stays reachable without paging. Not counted against
        # `limit`; has_more recomputed the same way as the prompt.
        compact_missing = [
            it
            for it in items
            if it.kind == "inbound_compact_summary"
            and not any(w.item_id == it.item_id for w in window)
        ]
        if compact_missing:
            window = [*window[:1], *compact_missing, *window[1:]]
            has_more = has_more and len(items) - 1 - len(compact_missing) > limit
    else:
        window, has_more = _window_before(items, before, limit)
    return TimelineResponse(items=window, msg_count=msg_count, has_more=has_more)
