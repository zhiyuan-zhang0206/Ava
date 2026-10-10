"""Timeline endpoint — /api/agents/{agent_id}/timeline.

Cold-load path only (page mount / agent switch). During a turn the frontend
updates from agent-published `timeline_snapshot` events; this endpoint just
serves the initial full view. Both render through the same
`base.agents.history.timeline.build_timeline_items`, so the cold load and the live
snapshots agree item-for-item.

This is a cold-load checkpoint reader (see `base.agents.history.checkpoint` for the shared
read contract). Read failures return 503; retained compact history has a
separate explicit endpoint and never substitutes for the live checkpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from psycopg import Connection
from pydantic import BaseModel

from base.agents.history.checkpoint import (
    CheckpointReadError,
    list_compact_boundary_checkpoint_ids,
    load_checkpoint_message_count,
    load_checkpoint_messages,
    load_checkpoint_messages_segment,
)
from base.agents.history.timeline import (
    TimelineItem,
    build_timeline_items,
    tail_window,
    timeline_default_limit,
)
from base.agents.impersonation.timeline import hydrate
from base.config import settings
from base.db import Database, InboundRow, agent_exists, list_inbound_messages
from gateway.agents.eval_guard import deny_isolated_result_read

router = APIRouter()
_log = logging.getLogger(__name__)
_REATTACHED_KINDS = frozenset({"system_prompt", "inbound_compact_summary"})


def _standing_head_note_ids(items: list[TimelineItem]) -> set[str]:
    """Item ids of the standing head notes: the contiguous ``system_marker``
    run immediately after the system prompt.

    These are the notes ``agent/graph/prompt/context_notes.py`` lays down at window
    establishment (exec timeout / timezone / cluster memory / agent id / agent
    memory / preloaded skills), rendered as ``system_marker`` items right
    behind the prompt. They are standing context of the same class as the
    prompt itself: ``_initial_window`` re-attaches them at the window head,
    and the paging paths treat them as re-attached context (a cursor that
    would select one must cross to the next older segment instead of looping
    on the re-attached head).

    The run requires ``source is not None`` (a real NoteTag): the catch-all
    fallback renders untagged HumanMessages — pre-tag checkpoint rows and
    retired markers that old data may still carry right after the prompt —
    as ``system_marker`` with ``source=None``, and treating those as head
    notes would re-attach stale rows to every window and skip them in paging.
    """
    head: set[str] = set()
    idx = 0
    # The current segment's prompt is always the plain "0.0" item (historical
    # segments carry segment-prefixed ids), and the standing notes directly
    # follow it. Historical segments have their SystemMessage stripped by
    # ``load_checkpoint_messages_segment``, so their standing notes sit at
    # items[0] — the run starts there when no prompt is present.
    while idx < len(items) and items[idx].kind != "system_prompt":
        idx += 1
    if idx < len(items):
        idx += 1  # past the prompt itself
    else:
        idx = 0  # no prompt (historical segment): its notes start at the front
    while (
        idx < len(items)
        and items[idx].kind == "system_marker"
        and items[idx].source is not None
        and items[idx].impersonation is None
    ):
        head.add(items[idx].item_id)
        idx += 1
    return head


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


class RetainedTimelineResponse(BaseModel):
    """One retained compact-history window, independent of live state.messages.

    The boundary identity describes the requested retained segment. Historical
    item cursors identify every item's own segment, including a crossed page.
    There is no live message count and these items must not merge into live state.
    """

    boundary_checkpoint_id: str | None
    items: list[TimelineItem]
    has_more: bool


def _read_unavailable(agent_id: int, exc: CheckpointReadError) -> HTTPException:
    _log.warning(
        "timeline checkpoint read unavailable for agent %s: %s (cause %s)",
        agent_id,
        type(exc).__name__,
        type(exc.__cause__).__name__,
    )
    return HTTPException(
        status_code=503, detail=f"Checkpoint history unavailable for agent {agent_id}"
    )


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
    before its oldest real item is re-attached context (the prompt, the
    standing head notes, compact summaries), return that small head once more
    while signaling the caller to append the next older segment.
    Frontend de-duplication converges repeated standing context, and returning
    it here guarantees a compact summary cannot fall between page boundaries.
    """
    idx = next((i for i, item in enumerate(items) if item.item_id == before), None)
    if idx is None:
        return [], False, False
    head_note_ids = _standing_head_note_ids(items)
    if all(item.kind in _REATTACHED_KINDS or item.item_id in head_note_ids for item in items[:idx]):
        return items[:idx], older_segment_available, True
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
    db: Database,
    agent_id: int,
    boundary_ids: list[str],
    rank: int,
    limit: int,
    depth: int,
) -> tuple[list[TimelineItem], bool]:
    """Load and window one exact older segment."""
    if rank > len(boundary_ids) or not _depth_allows(rank, depth):
        return [], False
    items = _load_history_segment(db, agent_id, boundary_ids[rank - 1], rank, limit=limit)
    if not items:
        return [], False
    window, segment_has_more = tail_window(items, limit)
    return window, segment_has_more or _older_segment_available(rank, len(boundary_ids), depth)


def _load_history_segment(
    db: Database,
    agent_id: int,
    checkpoint_id: str,
    rank: int,
    *,
    limit: int | None = None,
    before: str | None = None,
) -> list[TimelineItem] | None:
    """Load and render one persisted segment; read/render failures stay visible."""
    if limit is None:
        limit = timeline_default_limit()
    try:
        messages = load_checkpoint_messages_segment(db, agent_id, checkpoint_id)
    except CheckpointReadError as exc:
        raise _read_unavailable(agent_id, exc) from exc
    if not messages:
        return None
    prefix = f"s{rank}.{checkpoint_id}"
    items, _ = build_timeline_items(messages, [], segment_prefix=prefix)
    return hydrate(db, items, agent_id, limit=limit, before=before)


def _load_boundary_ids(db: Database, agent_id: int, depth: int) -> list[str]:
    if depth == 0:
        return []
    try:
        return list_compact_boundary_checkpoint_ids(
            db,
            agent_id,
            limit=depth + 1 if depth > 0 else None,
        )
    except CheckpointReadError as exc:
        raise _read_unavailable(agent_id, exc) from exc


def _load_current_message_count(db: Database, agent_id: int) -> int:
    try:
        return load_checkpoint_message_count(db, agent_id)
    except CheckpointReadError as exc:
        raise _read_unavailable(agent_id, exc) from exc


def _historical_window(
    db: Database,
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
    segment_items = _load_history_segment(
        db, agent_id, checkpoint_id, rank, limit=limit, before=cursor.item_id()
    )
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
    head = window
    if not older_available:
        return head, False

    # Release the requested segment before materializing the cross-segment
    # target. At most one checkpoint segment is resident during a request.
    del segment_items
    older_rank = rank + 1
    older_window, older_has_more = _load_history_tail(
        db,
        agent_id,
        boundary_ids,
        older_rank,
        limit,
        depth,
    )
    return [*head, *older_window], older_has_more


def _missing_standing_context(
    items: list[TimelineItem], window: list[TimelineItem]
) -> tuple[list[TimelineItem], list[TimelineItem]]:
    """The standing head notes and compact summaries that fell off the tail `window`."""
    in_window = {window_item.item_id for window_item in window}
    # The standing head notes — the contiguous system_marker run right after
    # the prompt (exec timeout / timezone / cluster memory / agent id / agent
    # memory / preloaded skills, agent/graph/prompt/context_notes.py) — are the same
    # class of standing context as the prompt: laid down at window
    # establishment, they fall off the tail window for any conversation past
    # `limit` rendered items. Re-attach the missing ones right after the
    # prompt so a long conversation's head reads like a fresh window (user
    # report 2026-08-27: agent 2992's head showed only "system prompt ·
    # compact summary" while a fresh agent shows "system prompt · 2 memories ·
    # 3 system notes"). Deduped by item_id; not counted against `limit`.
    head_note_ids = _standing_head_note_ids(items)
    notes_missing = [
        item for item in items if item.item_id in head_note_ids and item.item_id not in in_window
    ]
    # Compact summaries are standing context too. Once the prompt is
    # re-attached, a cursor can never page to summaries older than it, so keep
    # every missing summary immediately after the head notes.
    compact_missing = [
        item
        for item in items
        if item.kind == "inbound_compact_summary" and item.item_id not in in_window
    ]
    return notes_missing, compact_missing


def _initial_window(
    items: list[TimelineItem], limit: int, *, historical_segments_available: bool
) -> tuple[list[TimelineItem], bool]:
    """Window the current segment while preserving its standing context."""
    window, has_more = tail_window(items, limit)
    # The system-prompt item (0.0) is the OLDEST item and falls off the tail
    # window for a long conversation. Re-attach it without counting it
    # against the page limit or creating a phantom older-page affordance.
    prompt = None
    if not any(item.item_id == "0.0" for item in window):
        prompt = next((item for item in items if item.kind == "system_prompt"), None)

    notes_missing, compact_missing = _missing_standing_context(items, window)

    reattached = (0 if prompt is None else 1) + len(notes_missing) + len(compact_missing)
    if reattached:
        # Standing context heads the window in reading order: prompt, head
        # notes, compact summaries, then the raw tail window.
        window = [
            *([prompt] if prompt is not None else []),
            *notes_missing,
            *compact_missing,
            *window,
        ]
        has_more = has_more and len(items) - reattached > limit
    return window, has_more or historical_segments_available


def _chat_anchors(conn: Connection[Any], agent_id: int) -> list[InboundRow]:
    """All chat inbound anchors — they drive the ts alignment of the timeline items."""
    # The 100_000 ceiling is a protective bound, not a page size: truncation
    # would drop alignment anchors, so the read stays a literal rather
    # than config (task #3696 exception inventory).
    return [
        row for row in list_inbound_messages(conn, agent_id, limit=100_000) if row.kind == "chat"
    ]


def _current_page_before(
    items: list[TimelineItem],
    cursor: _TimelineCursor | None,
    limit: int,
    depth: int,
    boundary_ids: list[str],
    msg_count: int,
) -> TimelineResponse | list[TimelineItem]:
    """A `before=` page of the current segment; the head still to extend with older history
    when the page crosses into a compact-history segment."""
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
    if not cross:
        return TimelineResponse(items=window, msg_count=msg_count, has_more=has_more)
    if not older_available:
        return TimelineResponse(items=window, msg_count=msg_count, has_more=False)
    return window


def _historical_response(
    db: Database,
    agent_id: int,
    before: str | None,
    cursor: _TimelineCursor | None,
    limit: int,
    boundary_ids: list[str],
    depth: int,
) -> TimelineResponse | None:
    """The page of a historical or malformed `before=` request; None for a live-checkpoint read."""
    # Historical pages do not deserialize the live checkpoint. Its exact
    # message count comes from the five-byte channel header instead.
    if before is not None and cursor is None:
        return TimelineResponse(
            items=[],
            msg_count=_load_current_message_count(db, agent_id),
            has_more=False,
        )
    if cursor is not None and cursor.checkpoint_id is not None:
        window, has_more = _historical_window(db, agent_id, cursor, limit, boundary_ids, depth)
        return TimelineResponse(
            items=window,
            msg_count=_load_current_message_count(db, agent_id),
            has_more=has_more,
        )
    return None


@router.get("/api/agents/{agent_id}/timeline", dependencies=[Depends(deny_isolated_result_read)])
def get_timeline(
    agent_id: int,
    request: Request,
    # `limit`'s range stays a protective constant (import-time Query bound;
    # task #3696 exception inventory); the default *window* is
    # display.timeline_default_limit.
    limit: int | None = Query(default=None, ge=1, le=1000),
    before: str | None = Query(default=None),
) -> TimelineResponse:
    """Timeline = raw view of LangGraph state.messages, one window at a time.

    Default (no `before`) returns the newest `limit` items; an omitted
    `limit` resolves to the configured `display.timeline_default_limit`
    (50 by default). Pass
    `before=<oldest item_id you hold>` to fetch the previous window for
    scroll-up history loading. `has_more` reports whether older items exist
    before the returned window.

    One checkpoint segment is built at a time; windowing trims the payload +
    the frontend render. A checkpoint read failure returns 503. Retained
    compact boundaries remain independently readable via /timeline/retained.
    """
    if limit is None:
        limit = timeline_default_limit()
    cursor = _parse_cursor(before) if before is not None else None
    historical_request = cursor is not None and cursor.checkpoint_id is not None
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        chat_anchors = (
            []
            if historical_request or (cursor is None and before is not None)
            else _chat_anchors(conn, agent_id)
        )
    depth = settings.gateway.timeline_compact_history
    db: Database = request.app.state.db
    boundary_ids = _load_boundary_ids(db, agent_id, depth)

    early = _historical_response(db, agent_id, before, cursor, limit, boundary_ids, depth)
    if early is not None:
        return early

    try:
        messages = load_checkpoint_messages(db, agent_id)
    except CheckpointReadError as exc:
        raise _read_unavailable(agent_id, exc) from exc
    items, msg_count = build_timeline_items(messages, chat_anchors)
    items = hydrate(db, items, agent_id, limit=limit, before=before)
    items.sort(key=lambda it: _item_sort_key(it.item_id))
    if before is None:
        window, has_more = _initial_window(
            items,
            limit,
            historical_segments_available=bool(boundary_ids),
        )
        return TimelineResponse(items=window, msg_count=msg_count, has_more=has_more)
    paged = _current_page_before(items, cursor, limit, depth, boundary_ids, msg_count)
    if isinstance(paged, TimelineResponse):
        return paged
    del messages, items
    older_window, has_more = _load_history_tail(db, agent_id, boundary_ids, 1, limit, depth)
    return TimelineResponse(items=[*paged, *older_window], msg_count=msg_count, has_more=has_more)


def _retained_cursor(before: str | None, checkpoint_id: str | None) -> _TimelineCursor | None:
    if before is None:
        return None
    cursor = _parse_cursor(before)
    if cursor is None or cursor.checkpoint_id is None:
        raise HTTPException(status_code=400, detail="Retained history requires a historical cursor")
    if checkpoint_id is not None and cursor.checkpoint_id != checkpoint_id:
        raise HTTPException(status_code=400, detail="Retained boundary and cursor do not match")
    return cursor


@router.get(
    "/api/agents/{agent_id}/timeline/retained", dependencies=[Depends(deny_isolated_result_read)]
)
def get_retained_timeline(
    agent_id: int,
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=1000),
    checkpoint_id: str | None = Query(default=None, max_length=_MAX_CURSOR_LENGTH),
    before: str | None = Query(default=None, max_length=_MAX_CURSOR_LENGTH),
) -> RetainedTimelineResponse:
    """Read retained compact history explicitly, without reading the live head.

    Omit checkpoint_id for the newest retained boundary, or select an exact
    retained boundary. Page using the oldest returned historical item_id.
    Never resumes, rewrites, or repairs the agent's execution checkpoint.
    """
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    db: Database = request.app.state.db
    depth = settings.gateway.timeline_compact_history
    boundary_ids = _load_boundary_ids(db, agent_id, depth)
    cursor = _retained_cursor(before, checkpoint_id)
    selected = cursor.checkpoint_id if cursor is not None else checkpoint_id
    if selected is None and not boundary_ids:
        return RetainedTimelineResponse(boundary_checkpoint_id=None, items=[], has_more=False)
    selected = selected or boundary_ids[0]
    if selected not in boundary_ids:
        raise HTTPException(status_code=404, detail="Retained compact boundary not found")
    if limit is None:
        limit = timeline_default_limit()
    if cursor is not None:
        items, has_more = _historical_window(db, agent_id, cursor, limit, boundary_ids, depth)
    else:
        rank = boundary_ids.index(selected) + 1
        items, has_more = _load_history_tail(db, agent_id, boundary_ids, rank, limit, depth)
    return RetainedTimelineResponse(boundary_checkpoint_id=selected, items=items, has_more=has_more)
