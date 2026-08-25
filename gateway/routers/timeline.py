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
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from gateway.routers._eval_guard import deny_isolated_result_read
from shared.checkpoint import (
    CheckpointReadError,
    list_compact_boundary_checkpoint_ids,
    load_checkpoint_message_count,
    load_checkpoint_messages,
    load_checkpoint_messages_segment,
)
from shared.config import settings
from shared.db import agent_exists, list_inbound_messages
from shared.timeline import (
    DEFAULT_TIMELINE_LIMIT,
    TimelineItem,
    build_timeline_items,
    tail_window,
)

router = APIRouter()
_log = logging.getLogger(__name__)
_REATTACHED_KINDS = frozenset({"system_prompt", "inbound_compact_summary"})
_MAX_CURSOR_LENGTH = 512
_MAX_CURSOR_INDEX_DIGITS = 20


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


@dataclass(frozen=True)
class _TimelineCursor:
    checkpoint_id: str | None
    msg_idx: int
    block_idx: int

    def item_id(self, segment_prefix: str = "") -> str:
        local_id = f"{self.msg_idx}.{self.block_idx}"
        return f"{segment_prefix}.{local_id}" if segment_prefix else local_id


def _parse_cursor(before: str) -> _TimelineCursor | None:
    """Parse current (`m.b`) or historical (`sK.cpid.m.b`) item ids.

    Historical ranks are syntax only. The checkpoint id is resolved against
    the current boundary index and determines the canonical rank used in the
    response, so a compact that shifts ranks cannot redirect a stale cursor.
    """
    if len(before) > _MAX_CURSOR_LENGTH:
        return None
    parts = before.split(".")
    if len(parts) == 2:
        msg, block = parts
        msg_idx = _parse_cursor_index(msg)
        block_idx = _parse_cursor_index(block)
        if msg_idx is None or block_idx is None:
            return None
        return _TimelineCursor(checkpoint_id=None, msg_idx=msg_idx, block_idx=block_idx)
    if len(parts) != 4:
        return None
    rank, checkpoint_id, msg, block = parts
    rank_value = rank[1:] if rank.startswith("s") else ""
    rank_idx = _parse_cursor_index(rank_value)
    msg_idx = _parse_cursor_index(msg)
    block_idx = _parse_cursor_index(block)
    if rank_idx is None or rank_idx == 0 or rank_value.startswith("0") or not checkpoint_id:
        return None
    if msg_idx is None or block_idx is None:
        return None
    return _TimelineCursor(
        checkpoint_id=checkpoint_id,
        msg_idx=msg_idx,
        block_idx=block_idx,
    )


def _parse_cursor_index(value: str) -> int | None:
    """Parse one bounded ASCII item-id integer without integer-limit errors."""
    if (
        not value
        or len(value) > _MAX_CURSOR_INDEX_DIGITS
        or not value.isascii()
        or not value.isdigit()
    ):
        return None
    try:
        return int(value)
    except ValueError:
        return None


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


def _window_or_cross(
    items: list[TimelineItem],
    before: str,
    limit: int,
    *,
    older_segment_available: bool,
) -> tuple[list[TimelineItem], bool, bool]:
    """Return a segment-local page or signal that the cursor is at its head.

    The frontend never uses re-attached context as a cursor. When every item
    before its oldest real item is re-attached context, that context is already
    held and the next useful page is the tail of the next older segment.
    """
    idx = next((i for i, item in enumerate(items) if item.item_id == before), None)
    if idx is None:
        return [], False, False
    if all(item.kind in _REATTACHED_KINDS for item in items[:idx]):
        return [], older_segment_available, True
    start = max(0, idx - limit) if limit > 0 else 0
    return items[start:idx], start > 0 or older_segment_available, False


def _depth_allows(rank: int, depth: int) -> bool:
    return depth == -1 or 0 < rank <= depth


def _older_segment_available(rank: int, boundary_count: int, depth: int) -> bool:
    older_rank = rank + 1
    return older_rank <= boundary_count and _depth_allows(older_rank, depth)


def _item_sort_key(item_id: str) -> tuple[int, int]:
    """Order items by their logical append position (msg_idx, block_idx) parsed
    from item_id — numeric, so "10.0" follows "2.0". This is the order
    build_timeline_items already emits in; sorting by created_at would reorder
    on a clock skew now that items carry real wall-clock timestamps."""
    *_, msg_idx, block_idx = item_id.split(".")
    return (int(msg_idx), int(block_idx))


def _load_history_tail(
    agent_id: int,
    checkpoint_id: str,
    rank: int,
    limit: int,
    boundary_count: int,
    depth: int,
) -> tuple[list[TimelineItem], bool]:
    """Load and window one exact compact boundary, tolerating bad blobs."""
    items = _load_history_segment(agent_id, checkpoint_id, rank)
    if not items:
        return [], False
    window, segment_has_more = tail_window(items, limit)
    return window, segment_has_more or _older_segment_available(rank, boundary_count, depth)


def _load_history_segment(
    agent_id: int, checkpoint_id: str, rank: int
) -> list[TimelineItem] | None:
    """Load and render one persisted segment; damaged data is terminal."""
    try:
        messages = load_checkpoint_messages_segment(agent_id, checkpoint_id)
    except CheckpointReadError as exc:
        _log.warning(
            "timeline compact history: checkpoint read failed for agent %s boundary %s: %r",
            agent_id,
            checkpoint_id,
            exc,
        )
        return None
    if not messages:
        return None
    prefix = f"s{rank}.{checkpoint_id}"
    try:
        items, _ = build_timeline_items(messages, [], segment_prefix=prefix)
    except (TypeError, ValueError) as exc:
        _log.warning(
            "timeline compact history: checkpoint render failed for agent %s boundary %s: %r",
            agent_id,
            checkpoint_id,
            exc,
        )
        return None
    return items


def _load_boundary_ids(agent_id: int, depth: int) -> list[str]:
    if depth == 0:
        return []
    try:
        return list_compact_boundary_checkpoint_ids(agent_id)
    except CheckpointReadError as exc:
        _log.warning(
            "timeline compact history: boundary index read failed for agent %s: %r",
            agent_id,
            exc,
        )
        return []


def _load_current_message_count(agent_id: int) -> int:
    try:
        return load_checkpoint_message_count(agent_id)
    except CheckpointReadError as exc:
        _log.warning(
            "timeline current message count read failed for agent %s: %r",
            agent_id,
            exc,
        )
        return 0


def _historical_window(
    agent_id: int,
    cursor: _TimelineCursor,
    limit: int,
    boundary_ids: list[str],
    depth: int,
) -> tuple[list[TimelineItem], bool]:
    """Page one exact boundary; a stale display rank never selects content."""
    checkpoint_id = cursor.checkpoint_id
    if checkpoint_id is None:
        return [], False
    try:
        rank = boundary_ids.index(checkpoint_id) + 1
    except ValueError:
        return [], False
    if not _depth_allows(rank, depth):
        return [], False
    segment_items = _load_history_segment(agent_id, checkpoint_id, rank)
    if not segment_items:
        return [], False
    segment_prefix = f"s{rank}.{checkpoint_id}"
    older_available = _older_segment_available(rank, len(boundary_ids), depth)
    window, has_more, cross = _window_or_cross(
        segment_items,
        cursor.item_id(segment_prefix),
        limit,
        older_segment_available=older_available,
    )
    if not cross:
        return window, has_more
    if not older_available:
        return [], False

    # Release the requested segment before materializing the cross-segment
    # target. At most one checkpoint segment is resident during a request.
    del segment_items
    older_rank = rank + 1
    return _load_history_tail(
        agent_id,
        boundary_ids[older_rank - 1],
        older_rank,
        limit,
        len(boundary_ids),
        depth,
    )


def _initial_window(
    items: list[TimelineItem], limit: int, *, historical_segments_available: bool
) -> tuple[list[TimelineItem], bool]:
    """Window the current segment while preserving its standing context."""
    window, has_more = tail_window(items, limit)
    # The system-prompt item (0.0) is the OLDEST item and falls off the tail
    # window for a long conversation. Re-attach it without counting it
    # against the page limit or creating a phantom older-page affordance.
    if not any(item.item_id == "0.0" for item in window):
        prompt = next((item for item in items if item.kind == "system_prompt"), None)
        if prompt is not None:
            window = [prompt, *window]
            has_more = has_more and len(items) - 1 > limit

    # Compact summaries are standing context too. Once the prompt is
    # re-attached, a cursor can never page to summaries older than it, so keep
    # every missing summary immediately after the prompt.
    compact_missing = [
        item
        for item in items
        if item.kind == "inbound_compact_summary"
        and not any(window_item.item_id == item.item_id for window_item in window)
    ]
    if compact_missing:
        window = [*window[:1], *compact_missing, *window[1:]]
        has_more = has_more and len(items) - 1 - len(compact_missing) > limit
    return window, has_more or historical_segments_available


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

    One checkpoint segment is built at a time; windowing trims the payload +
    the frontend render. A checkpoint read failure renders an empty view + 200
    (cold-load tolerance, see `shared.checkpoint`).
    """
    cursor = _parse_cursor(before) if before is not None else None
    historical_request = cursor is not None and cursor.checkpoint_id is not None
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        # All chat inbound anchors, no limit — anchors drive ts alignment.
        chat_anchors = (
            []
            if historical_request or (cursor is None and before is not None)
            else [
                row
                for row in list_inbound_messages(conn, agent_id, limit=100_000)
                if row.kind == "chat"
            ]
        )
    depth = settings.gateway.timeline_compact_history
    boundary_ids = _load_boundary_ids(agent_id, depth)

    # Historical pages do not deserialize the live checkpoint. Its exact
    # message count comes from the five-byte channel header instead.
    if before is not None and cursor is None:
        return TimelineResponse(
            items=[],
            msg_count=_load_current_message_count(agent_id),
            has_more=False,
        )
    if historical_request and cursor is not None:
        window, has_more = _historical_window(
            agent_id,
            cursor,
            limit,
            boundary_ids,
            depth,
        )
        return TimelineResponse(
            items=window,
            msg_count=_load_current_message_count(agent_id),
            has_more=has_more,
        )

    try:
        messages = load_checkpoint_messages(agent_id)
    except CheckpointReadError as exc:
        _log.warning("timeline cold load: checkpoint read failed for agent %s: %r", agent_id, exc)
        messages = []
    items, msg_count = build_timeline_items(messages, chat_anchors)
    items.sort(key=lambda it: _item_sort_key(it.item_id))
    if before is None:
        window, has_more = _initial_window(
            items,
            limit,
            historical_segments_available=bool(boundary_ids),
        )
    else:
        if cursor is None or cursor.checkpoint_id is not None:
            return TimelineResponse(items=[], msg_count=msg_count, has_more=False)
        # Depth 0 is the compatibility posture: preserve the exact current-
        # segment paging rule, including pages that contain standing context.
        # No boundary query or history branch runs.
        if depth == 0:
            window, has_more = _window_before(items, cursor.item_id(), limit)
            return TimelineResponse(items=window, msg_count=msg_count, has_more=has_more)
        older_available = bool(boundary_ids) and _depth_allows(1, depth)
        window, has_more, cross = _window_or_cross(
            items,
            cursor.item_id(),
            limit,
            older_segment_available=older_available,
        )
        if cross:
            if not older_available:
                return TimelineResponse(items=[], msg_count=msg_count, has_more=False)
            del messages, items
            window, has_more = _load_history_tail(
                agent_id,
                boundary_ids[0],
                1,
                limit,
                len(boundary_ids),
                depth,
            )
    return TimelineResponse(items=window, msg_count=msg_count, has_more=has_more)
