"""Raw-context strip reads for the run timeline (P4-2, task #4023).

The window endpoint's ``messages`` field projection lives here together with
its on-demand text read (``GET .../run-timeline/message``). Both render the
SAME ``shared.timeline.build_timeline_items`` projection the console timeline
serves, so humans and agents read one rendering of the context; per-message
text stays out of the window response and is served by the detail route.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from langchain_core.messages import BaseMessage

from gateway.routers._eval_guard import deny_isolated_result_read
from gateway.schemas.run_timeline import (
    RunTimelineMessage,
    RunTimelineMessageDetailPart,
    RunTimelineMessageDetails,
    RunTimelineMessageKind,
    RunTimelineMessagePart,
    RunTimelineMessagePartKind,
)
from shared.config import settings
from shared.db import InboundRow
from shared.log import logger
from shared.timeline import TimelineItem, build_timeline_items, needs_chat_anchors

router = APIRouter()


# --- raw context strip (P4-2, task #4023) -----------------------------------
#
# The strip's per-message entries come from the SAME projection the console
# timeline serves (`shared.timeline.build_timeline_items` over checkpoint
# segments), so humans and agents read one rendering of the context. Message
# text stays out of the window response; `GET .../run-timeline/message`
# serves it on demand for the panel.

# Compact-history walk cap for the strip: a window spanning more than three
# compact boundaries is a multi-session view where per-message context loses
# its meaning; stopping there (and flagging the truncation) reads more
# honestly than stitching arbitrary history. Constant, not config: raising it
# multiplies checkpoint reads per request, not display value.
_MESSAGE_SEGMENT_WALK_MAX = 3

# Messages predating `ava_created_at` render with epoch-anchored synthesized
# timestamps (shared.timeline's legacy anchor path). They cannot be placed on
# a real time axis; the strip excludes them and reports truncation instead of
# stacking them at the window's left edge.
_LEGACY_TS_FLOOR = datetime(2020, 1, 1, tzinfo=UTC)


# --- short-TTL segment cache (review condition 10, 2026-09-19) -------------
#
# Every strip request re-reads the agent's current checkpoint segment (plus a
# compact-history segment and the boundary list when the window reaches past
# it); on a busy agent that is ~0.9s of the endpoint's cost, and pan/zoom
# fires one request per settled gesture frame — multiple requests a second.
# The message-details route reads the same segments, so a tiny per-process
# cache removes the repeat cost for both. The shared checkpoint layer already
# promises what this relies on: cold-load reads may serve the last committed
# snapshot, and a slightly stale view is acceptable on these paths.
#
# TTLs and the entry bound are cache tuning, not display behavior — constants
# with reasons, like the walk cap above.
_CACHE_TTL_CURRENT_S = 5.0  # the current segment grows as the agent commits
_CACHE_TTL_SEALED_S = 60.0  # a sealed compact-history segment is immutable
_CACHE_TTL_BOUNDARIES_S = 30.0  # boundary ids change only on compact
# Memory bound: one agent's strip working set is its current segment plus up
# to three history segments, and the gateway is shared by concurrently viewed
# agents; twelve entries cover two such working sets plus headroom, without
# letting segment-sized payloads accumulate per process.
_CACHE_MAX_ENTRIES = 12


class SegmentReadCache:
    """Tiny per-process LRU + TTL cache for the checkpoint segments the
    strip reads. `clock` is injectable for tests."""

    def __init__(self, max_entries: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._entries: dict[tuple[object, ...], tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._clock = clock

    def clear(self) -> None:
        """Drop every entry (tests; an operator has no reason to call this)."""
        with self._lock:
            self._entries.clear()

    def get(self, key: tuple[object, ...], ttl_s: float, load: Callable[[], object]) -> object:
        """The cached value for *key* when younger than *ttl_s*, else a fresh
        load (done outside the lock — a concurrent miss may load in parallel,
        last write wins)."""
        now = self._clock()
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None and now - hit[0] <= ttl_s:
                self._entries.pop(key)
                self._entries[key] = hit  # refresh LRU position
                return hit[1]
        value = load()
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = (now, value)
            while len(self._entries) > self._max_entries:
                self._entries.pop(next(iter(self._entries)))
        return value


_STRIP_CACHE = SegmentReadCache(_CACHE_MAX_ENTRIES)


def _cached_current_messages(agent_id: int) -> list[BaseMessage]:
    from shared.checkpoint import load_checkpoint_messages

    return cast(
        "list[BaseMessage]",
        _STRIP_CACHE.get(
            ("current", agent_id),
            _CACHE_TTL_CURRENT_S,
            lambda: load_checkpoint_messages(agent_id),
        ),
    )


def _cached_boundaries(agent_id: int) -> list[str]:
    from shared.checkpoint import list_compact_boundary_checkpoint_ids

    return cast(
        "list[str]",
        _STRIP_CACHE.get(
            ("boundaries", agent_id),
            _CACHE_TTL_BOUNDARIES_S,
            lambda: list_compact_boundary_checkpoint_ids(agent_id),
        ),
    )


def _cached_segment_messages(agent_id: int, boundary: str) -> list[BaseMessage]:
    from shared.checkpoint import load_checkpoint_messages_segment

    return cast(
        "list[BaseMessage]",
        _STRIP_CACHE.get(
            ("segment", agent_id, boundary),
            _CACHE_TTL_SEALED_S,
            lambda: load_checkpoint_messages_segment(agent_id, boundary),
        ),
    )


_PART_KIND_BY_ITEM: dict[str, RunTimelineMessagePartKind] = {
    "agent_reasoning": "think",
    "agent_chat": "text",
    "agent_code": "call",
    "code_output": "out",
    "system_marker": "note",
    "system_prompt": "prompt",
    "inbound_compact_summary": "compact",
    "inbound_compact_request": "compact",
    "inbound_chat": "inbound",
    "attach": "attach",
}

_MESSAGE_KIND_BY_ITEM: dict[str, RunTimelineMessageKind] = {
    "code_output": "exec",
    "system_marker": "note",
    "system_prompt": "prompt",
    "inbound_chat": "inbound",
    "attach": "attach",
    "inbound_compact_summary": "compact",
    "inbound_compact_request": "compact",
}

_AI_ITEM_KINDS = frozenset({"agent_reasoning", "agent_chat", "agent_code"})


def _message_part_kind(item_kind: str) -> RunTimelineMessagePartKind:
    """Strip part kind for one timeline item kind (fail-visible on unknown)."""
    mapped = _PART_KIND_BY_ITEM.get(item_kind)
    if mapped is None:
        raise ValueError(f"run-timeline strip: unmapped timeline item kind {item_kind!r}")
    return mapped


def _message_kind(item_kinds: set[str]) -> RunTimelineMessageKind:
    """Message-level kind for one item group (fail-visible on unknown mixes)."""
    if item_kinds <= _AI_ITEM_KINDS:
        return "ai"
    if len(item_kinds) == 1:
        mapped = _MESSAGE_KIND_BY_ITEM.get(next(iter(item_kinds)))
        if mapped is not None:
            return mapped
    raise ValueError(f"run-timeline strip: unmapped item kinds {sorted(item_kinds)!r}")


def _group_strip_items(items: list[TimelineItem]) -> list[tuple[str, list[TimelineItem]]]:
    """Group rendered items by their message; keys are the strip identities."""
    groups: dict[str, list[TimelineItem]] = {}
    for item in items:
        stem = ".".join(item.item_id.split(".")[:-1])
        if not stem:
            raise ValueError(f"run-timeline strip: malformed item id {item.item_id!r}")
        key = stem if "." in stem else f"c.{stem}"
        groups.setdefault(key, []).append(item)
    return list(groups.items())


def _group_ts(group: list[TimelineItem]) -> datetime | None:
    """The group's wall-clock — its first stamped item (blocks share one ts)."""
    for item in group:
        if item.created_at:
            return datetime.fromisoformat(item.created_at)
    return None


def _message_from_group(key: str, group: list[TimelineItem]) -> RunTimelineMessage:
    parts: list[RunTimelineMessagePart] = []
    for item in group:
        part_kind = _message_part_kind(item.kind)
        chars = len(item.payload)
        if parts and parts[-1].kind == part_kind:
            parts[-1] = RunTimelineMessagePart(kind=part_kind, chars=parts[-1].chars + chars)
        else:
            parts.append(RunTimelineMessagePart(kind=part_kind, chars=chars))
    return RunTimelineMessage(
        key=key,
        idx=int(key.rsplit(".", 1)[-1]),
        ts=_group_ts(group),
        kind=_message_kind({item.kind for item in group}),
        source=next((item.source for item in group if item.source), None),
        chars=sum(part.chars for part in parts),
        parts=parts,
    )


def _chat_inbound_anchors(agent_id: int) -> list[InboundRow]:
    """Chat inbound rows backing legacy ts alignment — read only when the
    segment still carries legacy rows (`needs_chat_anchors`); the same source
    and bound the console timeline reads."""
    from shared.db import list_inbound_messages, pool

    db_pool = pool(autocommit=True)
    with db_pool.connection() as conn:
        return [
            row
            for row in list_inbound_messages(conn, agent_id, limit=100_000)
            if row.kind == "chat"
        ]


def _placed_min_ts(groups: list[tuple[str, list[TimelineItem]]]) -> datetime | None:
    """Earliest placeable ts in a group set (legacy synthetic values excluded)."""
    stamps = [
        ts
        for ts in (_group_ts(group) for _, group in groups)
        if ts is not None and ts >= _LEGACY_TS_FLOOR
    ]
    return min(stamps) if stamps else None


def _strip_messages_for_window(
    agent_id: int, window_start: datetime, window_end: datetime
) -> tuple[list[RunTimelineMessage], bool]:
    """Assemble the raw-context strip for one window from checkpoint segments.

    The current segment is the primary read; compact-history segments are
    walked newest-first only when the window reaches before the current
    segment's earliest placeable message. Truncation (message budget or the
    segment walk cap) is always reported, never silent.
    """
    budget = settings.display.run_timeline_messages_max
    truncated = False

    current = _cached_current_messages(agent_id)
    anchors = _chat_inbound_anchors(agent_id) if needs_chat_anchors(current) else []
    current_items, _ = build_timeline_items(current, anchors)
    groups = _group_strip_items(current_items)

    current_min = _placed_min_ts(groups)
    if current_min is None or current_min > window_start:
        boundaries = _cached_boundaries(agent_id)
        covered = False
        for rank, boundary in enumerate(boundaries[:_MESSAGE_SEGMENT_WALK_MAX], start=1):
            segment = _cached_segment_messages(agent_id, boundary)
            if not segment:
                continue
            segment_items, _ = build_timeline_items(
                segment, [], segment_prefix=f"s{rank}.{boundary}"
            )
            segment_groups = _group_strip_items(segment_items)
            groups.extend(segment_groups)
            segment_min = _placed_min_ts(segment_groups)
            if segment_min is not None and segment_min <= window_start:
                covered = True
                break
        if not covered and len(boundaries) > _MESSAGE_SEGMENT_WALK_MAX:
            truncated = True

    placed: list[RunTimelineMessage] = []
    unplaceable = False
    for key, group in groups:
        ts = _group_ts(group)
        if ts is not None and ts < _LEGACY_TS_FLOOR:
            unplaceable = True
            continue
        if ts is not None and not (window_start <= ts <= window_end):
            continue
        placed.append(_message_from_group(key, group))
    if unplaceable:
        truncated = True
    placed.sort(key=lambda message: (message.ts is not None, message.ts or window_start))
    if len(placed) > budget:
        placed = placed[-budget:]
        truncated = True
    return placed, truncated


def strip_for_window_or_none(
    agent_id: int, window_start: datetime, window_end: datetime
) -> tuple[list[RunTimelineMessage] | None, bool | None]:
    """The strip read with the endpoint's degrade posture (narrative/inbounds)."""
    try:
        return _strip_messages_for_window(agent_id, window_start, window_end)
    except Exception:
        logger.exception("run-timeline strip read failed for agent {}", agent_id)
        return None, None


def _strip_message_group(agent_id: int, key: str) -> list[TimelineItem]:
    """Resolve one strip key to its items; 404 for unknown or malformed keys."""

    def not_found() -> HTTPException:
        return HTTPException(status_code=404, detail=f"message {key} not found")

    if key.startswith("c."):
        current = _cached_current_messages(agent_id)
        anchors = _chat_inbound_anchors(agent_id) if needs_chat_anchors(current) else []
        items, _ = build_timeline_items(current, anchors)
    elif key.startswith("s") and "." in key:
        rank, rest = key.split(".", 1)
        if "." not in rest:
            raise not_found()
        boundary, _idx = rest.rsplit(".", 1)
        segment = _cached_segment_messages(agent_id, boundary)
        if not segment:
            raise not_found()
        items, _ = build_timeline_items(segment, [], segment_prefix=f"{rank}.{boundary}")
    else:
        raise not_found()
    for group_key, group in _group_strip_items(items):
        if group_key == key:
            return group
    raise not_found()


@router.get(
    "/api/agents/{agent_id}/run-timeline/message",
    dependencies=[Depends(deny_isolated_result_read)],
)
def get_run_timeline_message(
    agent_id: int,
    key: Annotated[str, Query()],
    full: Annotated[bool, Query()] = False,  # noqa: FBT002 — FastAPI query param
) -> RunTimelineMessageDetails:
    """One strip message's text — the on-demand read behind the panel.

    ``key`` is the strip's stable message identity (``c.<idx>`` /
    ``s<rank>.<boundary>.<idx>``). Parts longer than
    ``display.run_timeline_message_text_max`` come back clipped with
    ``content_truncated``; refetch with ``full=true`` for the uncut text.
    """
    group = _strip_message_group(agent_id, key)
    text_max = settings.display.run_timeline_message_text_max
    parts: list[RunTimelineMessageDetailPart] = []
    content_truncated = False
    for item in group:
        text = item.payload
        clipped = not full and len(text) > text_max
        if clipped:
            text = text[:text_max]
            content_truncated = True
        parts.append(
            RunTimelineMessageDetailPart(
                kind=_message_part_kind(item.kind),
                chars=len(item.payload),
                text=text,
                text_truncated=clipped,
            )
        )
    return RunTimelineMessageDetails(
        key=key,
        kind=_message_kind({item.kind for item in group}),
        ts=_group_ts(group),
        source=next((item.source for item in group if item.source), None),
        chars=sum(part.chars for part in parts),
        parts=parts,
        content_truncated=content_truncated,
    )
