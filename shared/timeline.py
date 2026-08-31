"""Timeline view of LangGraph state.messages — wire type + pure renderer.

Both the producer (the agent, which renders from its in-memory state at each
node enter) and the gateway (`GET /api/agents/{id}/timeline`, which renders
from the checkpoint it reads from disk) call `build_timeline_items` on the
same rule, and both serialize `TimelineItem` as the response model. Living in
`shared/` (leaf) lets both import it without an agent <-> gateway package
cycle.

Why a single render function: streaming delta events render directly on the
frontend, but a committed inbound HumanMessage renders only via a snapshot.
Having the agent build the snapshot from its in-memory state removes the old
race where the gateway re-read the checkpoint before the async commit landed
and produced a snapshot missing the just-claimed inbound.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCall,
)
from pydantic import BaseModel

from shared.config import now_timestamp, settings
from shared.db import InboundRow
from shared.lm.content import ContentBlock, content_blocks
from shared.lm.reasoning import to_canonical_reasoning
from shared.message_kwargs import (
    AvaMessageKwargs,
    AvaMsgType,
    NoteTag,
    message_addl_kwargs,
    message_content,
    read_ava_kwargs,
)
from shared.sdk_call_extract import SdkCall, extract_sdk_calls


class TimelineItem(BaseModel):
    """One element of a timeline view of LangGraph state.messages, using the
    inbound_messages table only as ts anchor.

    `item_id` is the stable key coordinating frontend timeline with
    streaming SSE; the current segment uses `f"{msg_idx}.{block_idx}"`
    (msg_idx = position in state.messages; for other message types
    block_idx=0). Cold-loaded compact history prefixes that local position
    with `s<rank>.<boundary_checkpoint_id>.`; it never enters the live SSE
    merge path. For an
    AIMessage, text/thinking are content blocks (block_idx = their content
    position) and each tool call gets `block_idx = <number of text/thinking
    content blocks> + <ordinal in msg.tool_calls>` — a provider-agnostic
    rule derived from langchain's normalized `tool_calls` view, so it holds
    whether the provider carries tool calls inside `content` (anthropic
    `tool_use` blocks) or only in the separate `tool_calls` list
    (gemini / openai). During streaming, the frontend uses `*_start`
    event's `item_id` to create items; after commit, the same rule
    recomputes id, and matching ids on both sides means the same logical
    item. On merge, the snapshot version wins, avoiding race bugs from
    ts-heuristic matching.
    """

    item_id: str
    kind: Literal[
        "inbound_chat",
        "inbound_compact_summary",
        "inbound_compact_request",
        "attach",
        "agent_chat",
        "agent_code",
        "agent_reasoning",
        "code_output",
        "system_prompt",
        "system_marker",
    ]
    source: str | None = None
    payload: str  # rendered content
    created_at: str | None = None  # ISO-8601
    inbound_id: int | None = None
    # Reasoning-block summary on `agent_reasoning` items (None elsewhere).
    # Drives the collapsed-reasoning chip ("Thought for 8s / 1.2k tokens") so
    # the user keeps a sense of the thinking without reading it.
    # `reasoning_ms` is per-block: the real wall-clock the llm node measured
    # for that thinking block (agent/graph/_callbacks.py) and persisted on the
    # message — a turn with several thinking blocks carries one value per
    # block. `reasoning_tokens` stays turn-level (usage_metadata reports one
    # total) and sits on the first thinking item only.
    reasoning_ms: int | None = None
    reasoning_tokens: int | None = None
    # Wall-clock the code ran, set only on `code_output` items (None elsewhere).
    # Read from the exec_output message's ava_exec_ms (agent/graph/_exec.py).
    # Drives the collapsed-output chip ("ran in 1.3s").
    exec_ms: int | None = None
    # Whether the frontend chip shows this item's wall-clock ts. True everywhere
    # except system_marker notes that read as standing context rather than
    # events (memory recall + the one-time guidance notes) — see
    # `_NO_TIMESTAMP_NOTE_TAGS`. Default True keeps every other kind unchanged.
    show_timestamp: bool = True
    # Image urls on a multimodal `inbound_chat` / `attach` item (None
    # elsewhere): inbound_chat = gateway-relative urls from ava_image_urls
    # (via assetUrl); attach = data URIs from content blocks (rendered raw).
    images: list[str] | None = None
    # Per-image caption lines on an `attach` item, aligned 1:1 with `images`:
    # the backend-generated "- [N] name (mime, size) — \"label\"" line that
    # precedes each image block in the message content, so the frontend can
    # interleave every thumbnail with its own label. None on other kinds and on
    # legacy attach messages whose caption was a single text block (no pairing
    # information survives there).
    image_captions: list[str] | None = None
    # SDK calls extracted from agent_code payload via AST parsing (None on
    # other kinds). Drives the collapsed-code chip ("files.read x3" etc.)
    # with zero false positives from string literals or comments.
    sdk_calls: list[SdkCall] | None = None
    # Wall-clock the code-generation took, set only on `agent_code` items (None
    # elsewhere). Read from the AIMessage's ava_code_ms_by_block (keyed by code
    # block_idx). Drives the detail-block chip "Wrote code for Xs".
    code_elapsed_ms: int | None = None


# Items after the same inbound anchor are offset by a microsecond increment to
# preserve relative order without colliding with the next real-ts anchor
# (assuming two inbounds are at least 1 second apart, a million micro-offset
# items fit between).
_LG_TS_STEP_US = 1

# system_note tags whose chip hides the wall-clock ts: memory recall (standing
# ambient context) and the one-time guidance notes (compact reminder, the SDK
# hint, the agent-reply pointer, the silent idle-continue). They read as
# context, not events, so a timestamp is noise. Heartbeats and lifecycle_*
# transitions are real events and keep their ts (the default True).
_NO_TIMESTAMP_NOTE_TAGS = frozenset(
    {
        NoteTag.MEMORY,
        NoteTag.AGENT_ID,
        NoteTag.COMPACT_REMINDER,
        NoteTag.HISTORY_DUMP,
        NoteTag.SDK_HINT,
        NoteTag.AGENT_REPLY,
        NoteTag.SILENT_IDLE_CONTINUE,
        NoteTag.SECURITY,
        # Historical heartbeat-pause notes are one-time guidance, same family
        # as the security note beside it.
        NoteTag.HEARTBEAT_PAUSE,
        NoteTag.CONTEXT,
        NoteTag.PROJECT_SKILLS,
        NoteTag.PRELOADED_SKILLS,
        NoteTag.AGENT_MEMORY,
        NoteTag.EXEC_TIMEOUT,
        NoteTag.TIMEZONE,
    }
)
# Deliberately absent: NoteTag.NEW_SKILLS. Its siblings PROJECT_SKILLS /
# PRELOADED_SKILLS are standing head content whose ts only says when the window
# opened, but a drift note reports that a skill appeared at a moment mid-window —
# and this note is the only place that install is visible at all, so the ts is
# the information, not noise.


def _inbound_text(content: str | list[str | dict[str, Any]]) -> str:
    """The renderable text of an inbound message.

    A plain-text inbound has string content. A multimodal inbound has list
    content — a text block plus native image blocks; join the text blocks and
    drop the image blocks (their base64 is never rendered; the timeline shows
    thumbnails from ava_image_urls instead). Any non-text block a future writer
    adds is ignored here rather than str()-d in.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            text
            for b in content_blocks(content)
            if isinstance(b, dict)
            and b.get("type") == "text"
            and isinstance(text := b.get("text"), str)
        )
    return str(content)


def needs_chat_anchors(messages: Sequence[BaseMessage]) -> bool:
    """Whether rendering *messages* consumes chat inbound anchors at all.

    ``build_timeline_items`` matches each INBOUND HumanMessage to its
    ``kind='chat'`` row by ``ava_inbound_id``; the anchor supplies the real ts
    for LEGACY rows that predate ``ava_created_at``. Modern messages carry
    their own ts and id, so an
    all-modern window renders identically with ``[]`` anchors — and querying
    the inbound table for it (one round trip per full-window snapshot, per
    node enter) is pure waste. Callers skip the DB query when this is False.

    Compacted histories are always all-modern (REMOVE_ALL wipes the legacy
    rows; the rebuilt head is freshly stamped), so the post-compact snapshots
    — the ones the frontend is waiting on to refresh the context panel —
    render without a single DB round trip.
    """
    for msg in messages:
        kwargs = read_ava_kwargs(msg)
        if kwargs.get("ava_msg_type") == AvaMsgType.INBOUND and not kwargs.get("ava_created_at"):
            return True
    return False


def build_timeline_items(
    messages: Sequence[BaseMessage],
    chat_anchors: list[InboundRow],
    *,
    start: int = 0,
    segment_prefix: str = "",
) -> tuple[list[TimelineItem], int]:
    """Render state.messages into timeline items + return msg_count.

    `chat_anchors` are the agent's `kind='chat'` inbound rows in created_at
    ascending order. An inbound HumanMessage carrying `ava_inbound_id` advances
    to that exact anchor; only a legacy message without the id consumes the
    next row positionally. This matters after compaction, where the surviving
    checkpoint starts after many historical DB rows. Pass `[]` when no inbound
    table is available (container / eval mode); modern inbound items still
    retain their embedded id and timestamp.

    `start` renders only `messages[start:]` (incremental snapshot path: the
    caller holds a per-agent cursor of the last published msg_idx). Item ids
    keep their ABSOLUTE msg_idx (`enumerate(..., start=start)`), so streaming
    SSE item_ids stay aligned. When `start > 0` the anchors are deliberately
    NOT consumed: modern messages carry their own `ava_created_at` (ts is
    never synthetic on the incremental path), and consuming anchors from
    position 0 would misalign the window's first inbound with a historical
    chat's anchor. The caller passes `[]`.

    msg_count is ALWAYS the full `len(messages)`, never the window length —
    the frontend's future-partial boundary (`msg_idx == msg_count`) depends
    on the full value. This is a hard invariant of the incremental design.

    `segment_prefix` is reserved for cold-loaded compact history. It prefixes
    every locally rendered `msg_idx.block_idx` after rendering, preserving the
    same message dispatch while keeping historical ids globally distinct.

    Dispatch errors (unrecognized message shape) raise — fail-loud surfaces
    a missing branch immediately instead of a silently truncated timeline.
    """
    items: list[TimelineItem] = []
    anchor_positions = {anchor.id: index for index, anchor in enumerate(chat_anchors)}
    next_anchor_idx = 0
    # Fallback anchor for "no inbound seen yet" — epoch 0 sorts these items
    # before all real-ts items. In practice only framework-INSERTed 'system'
    # source takes this path.
    current_anchor = datetime.fromtimestamp(0, tz=UTC)
    sub_offset = 0

    def next_ts(msg: BaseMessage | None = None) -> str:
        """The item's wall-clock ts: the message's own real `ava_created_at`
        (stamped at creation by the producing node) when present, else a
        synthetic anchor+microsecond offset for legacy messages persisted
        before the field existed. The offset advances only on the synthetic
        path, so a turn's blocks that share one real ts collapse onto that ts
        while legacy blocks still fan out by a microsecond for stable ordering."""
        if msg is not None:
            real = read_ava_kwargs(msg).get("ava_created_at")
            if real:
                return real
        nonlocal sub_offset
        sub_offset += _LG_TS_STEP_US
        return (current_anchor + timedelta(microseconds=sub_offset)).isoformat()

    msg_count = len(messages)
    # msg_idx is 1:1 with the position in state.messages; streaming SSE events
    # use the same `len(state.messages)` to compute item_id, keeping the two
    # sides' stable keys aligned. `start` keeps absolute positions on the
    # incremental path.
    for msg_idx, msg in enumerate(messages[start:], start=start):
        if isinstance(msg, SystemMessage):
            # The agent's system prompt — built once at birth, persisted as
            # state.messages[0], never streamed. created_at stays None: it has
            # no inbound anchor (the fallback anchor is epoch 0, which would
            # render as a 1970 timestamp), and it is always the first item.
            raw_content = message_content(msg)
            items.append(
                _system_prompt_item(
                    msg_idx, raw_content if isinstance(raw_content, str) else str(raw_content)
                )
            )
            continue
        raw_content = message_content(msg)
        content = raw_content if isinstance(raw_content, str) else str(raw_content)
        kwargs = read_ava_kwargs(msg)
        ava_type = kwargs.get("ava_msg_type")
        if ava_type == AvaMsgType.INBOUND:
            item, current_anchor, sub_offset, next_anchor_idx = _inbound_item(
                msg_idx,
                raw_content,
                kwargs,
                chat_anchors,
                anchor_positions,
                next_anchor_idx,
                current_anchor,
                sub_offset,
                next_ts,
            )
            items.append(item)
        elif ava_type == AvaMsgType.ATTACH:
            items.append(_attach_item(msg_idx, raw_content, next_ts(msg)))
        elif ava_type == AvaMsgType.EXEC_OUTPUT:
            items.append(
                _exec_output_item(msg_idx, content, next_ts(msg), kwargs.get("ava_exec_ms"))
            )
        elif ava_type == AvaMsgType.SYSTEM_NOTE:
            items.append(
                _system_note_item(msg_idx, content, kwargs.get("ava_note_tag"), next_ts(msg))
            )
        elif ava_type == AvaMsgType.COMPACT_SUMMARY:
            items.append(_compact_item(msg_idx, "inbound_compact_summary", content, next_ts(msg)))
        elif ava_type == AvaMsgType.COMPACT_REQUEST:
            items.append(_compact_item(msg_idx, "inbound_compact_request", content, next_ts(msg)))
        elif isinstance(msg, AIMessage):
            # AIMessage.content (ChatAnthropic + bind_tools shape) is a
            # list-of-blocks: thinking / text / tool_use each takes a slot;
            # one timeline item per block, block_idx = anthropic
            # content_block_index, aligned with streaming SSE item_id.
            items.extend(_ai_message_items(msg, msg_idx, next_ts))
        elif isinstance(msg, HumanMessage):
            items.append(_fallback_human_item(msg_idx, content, next_ts(msg)))
    if segment_prefix:
        items = [
            item.model_copy(update={"item_id": f"{segment_prefix}.{item.item_id}"})
            for item in items
        ]
    return items, msg_count


def _system_prompt_item(msg_idx: int, content: str) -> TimelineItem:
    """The agent's system prompt item (state.messages[0], never streamed)."""
    return TimelineItem(
        item_id=f"{msg_idx}.0",
        kind="system_prompt",
        source=None,
        payload=content,
        created_at=None,
        inbound_id=None,
    )


def _inbound_item(
    msg_idx: int,
    raw_content: str | list[str | dict[str, Any]],
    kwargs: AvaMessageKwargs,
    chat_anchors: list[InboundRow],
    anchor_positions: dict[int, int],
    next_anchor_idx: int,
    current_anchor: datetime,
    sub_offset: int,
    next_ts: Callable[[BaseMessage | None], str],
) -> tuple[TimelineItem, datetime, int, int]:
    """Render one inbound HumanMessage and advance the legacy anchor cursor.

    A multimodal inbound carries list content (a text block + base64 image
    blocks); render the text part only, never str()-ing the base64 into the
    payload. Its image urls ride on ava_image_urls. Returns the item plus the
    advanced anchor state.
    """
    if "ava_inbound_id" in kwargs:
        embedded_id = kwargs["ava_inbound_id"]
        if type(embedded_id) is not int or embedded_id <= 0:
            raise ValueError(f"inbound ava_inbound_id must be a positive int, got {embedded_id!r}")
        # Exact lookup is non-destructive: a missing, duplicate, or out-of-order
        # modern id cannot exhaust or rewind the cursor used by truly legacy
        # messages. A forward match does advance past compacted-away rows.
        anchor_idx = anchor_positions.get(embedded_id)
        nxt = chat_anchors[anchor_idx] if anchor_idx is not None else None
        if anchor_idx is not None:
            next_anchor_idx = max(next_anchor_idx, anchor_idx + 1)
    else:
        # Pre-ava_inbound_id checkpoints have no correlation key. Preserve the
        # historical best-effort positional fallback for those rows only.
        embedded_id = None
        nxt = chat_anchors[next_anchor_idx] if next_anchor_idx < len(chat_anchors) else None
        if nxt is not None:
            next_anchor_idx += 1
    if nxt is not None:
        current_anchor = nxt.created_at
        sub_offset = 0
    images = kwargs.get("ava_image_urls")
    item = TimelineItem(
        item_id=f"{msg_idx}.0",
        kind="inbound_chat",
        source=kwargs.get("ava_source"),
        payload=_inbound_text(raw_content),
        created_at=(
            kwargs.get("ava_created_at") or (current_anchor.isoformat() if nxt else next_ts(None))
        ),
        inbound_id=embedded_id if embedded_id is not None else (nxt.id if nxt else None),
        images=images if images else None,
    )
    return item, current_anchor, sub_offset, next_anchor_idx


def _attach_images(content: str | list[str | dict[str, Any]]) -> list[str] | None:
    """Data-URI image urls from attach ``image_url`` blocks; other media ignored."""
    if not isinstance(content, list):
        return None
    urls: list[str] = []
    for block in content_blocks(content):
        if not isinstance(block, dict) or block.get("type") != "image_url":
            continue
        # `image_url` is not a ContentBlock key — cast through the dict arm.
        image_url = cast("dict[str, Any] | None", block.get("image_url"))
        if image_url is None:
            continue
        url = image_url.get("url")
        if isinstance(url, str) and url.startswith("data:"):
            urls.append(url)
    return urls or None


def _attach_image_captions(content: str | list[str | dict[str, Any]]) -> list[str] | None:
    """Per-image caption lines of an attach message, aligned with ``_attach_images``.

    The modern pack (``shared/lm/attach.py``) interleaves content blocks —
    ``[text(notice), text(line1), image1, text(line2), image2, ...]`` — so each
    ``image_url`` block is immediately preceded by its own caption line: read
    the preceding text block and the pairing is structural, no text parsing.

    Legacy attach messages carried ONE caption text block followed by all image
    blocks; there the per-image lines cannot be recovered (skipped entries are
    indistinguishable), so this returns None and the frontend falls back to the
    legacy all-text-then-all-images layout.
    """
    if not isinstance(content, list):
        return None
    text_blocks = [
        block
        for block in content_blocks(content)
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    if len(text_blocks) <= 1:
        return None
    captions: list[str] = []
    last_text: str | None = None
    for block in content_blocks(content):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                last_text = text
        elif block.get("type") == "image_url":
            captions.append(last_text or "")
    return captions or None


def _attach_item(
    msg_idx: int, raw_content: str | list[str | dict[str, Any]], created_at: str
) -> TimelineItem:
    """Attachment message item — caption only; thumbnails ride on ``images`` (data URIs)."""
    return TimelineItem(
        item_id=f"{msg_idx}.0",
        kind="attach",
        source=None,
        payload=_inbound_text(raw_content),
        created_at=created_at,
        inbound_id=None,
        images=_attach_images(raw_content),
        image_captions=_attach_image_captions(raw_content),
    )


def _exec_output_item(msg_idx: int, content: str, created_at: str, exec_ms: Any) -> TimelineItem:
    """One tool result (`code_output`) item; exec_ms drives the ran-in chip."""
    return TimelineItem(
        item_id=f"{msg_idx}.0",
        kind="code_output",
        source=None,
        payload=content,
        created_at=created_at,
        inbound_id=None,
        exec_ms=exec_ms,
    )


def _system_note_item(msg_idx: int, content: str, note_tag: Any, created_at: str) -> TimelineItem:
    """A framework-injected system note (one-time guidance + lifecycle).

    The ava_note_tag (a NoteTag) is the discriminator the frontend dispatches
    on to pick a chip; it passes straight through as `source`. A note carrying
    no tag is pre-change historical data (the field is required now) —
    source=None then renders as the red UnknownMarkerChip, the same fail-loud
    treatment as the catch-all.
    """
    return TimelineItem(
        item_id=f"{msg_idx}.0",
        kind="system_marker",
        source=note_tag,
        payload=content,
        created_at=created_at,
        inbound_id=None,
        show_timestamp=note_tag not in _NO_TIMESTAMP_NOTE_TAGS,
    )


def _compact_item(
    msg_idx: int,
    kind: Literal["inbound_compact_summary", "inbound_compact_request"],
    content: str,
    created_at: str,
) -> TimelineItem:
    """A compact summary / compact request item; payload gets a ts prefix when
    `settings.general.message_timestamps` is on."""
    ts = f" {now_timestamp()}" if settings.general.message_timestamps else ""
    label = "Compact summary" if kind == "inbound_compact_summary" else "Compact request"
    return TimelineItem(
        item_id=f"{msg_idx}.0",
        kind=kind,
        source=None,
        payload=f"{label}{ts}:\n\n{content}",
        created_at=created_at,
        inbound_id=None,
    )


def _fallback_human_item(msg_idx: int, content: str, created_at: str) -> TimelineItem:
    """Catch-all for HumanMessages no explicit branch above claims.

    All current framework-injected HumanMessages are tagged with ava_msg_type
    via agent/messages.py helpers and take a branch above; anything landing
    here renders as a system_marker the frontend flags red (UnknownMarkerChip)
    — fail-loud, don't silently mis-render. Retired marker types also land
    here: e.g. the old LLM-cancel marker (cancels no longer record anything),
    or pre-tag system notes (old ava_msg_type='lifecycle' / a system_note with
    no ava_note_tag), which a pre-change checkpoint may still carry — a red
    chip on those rare historical rows is acceptable.
    """
    return TimelineItem(
        item_id=f"{msg_idx}.0",
        kind="system_marker",
        source=None,
        payload=content,
        created_at=created_at,
        inbound_id=None,
    )


# Default tail-window size for the timeline (cold-load endpoint default +
# the agent-published snapshot trim). The unit is timeline items (one turn
# fans out into reasoning/code/output items), not state.messages, so this is
# several screenfuls. The same value drives both producers so a streaming
# turn always lands inside the window the frontend already holds.
DEFAULT_TIMELINE_LIMIT = 50


def tail_window(items: list[TimelineItem], limit: int) -> tuple[list[TimelineItem], bool]:
    """Return the newest `limit` items + whether older items exist before them.

    `limit <= 0` or fewer items than the limit returns everything with
    `has_more=False`.
    """
    if limit <= 0 or len(items) <= limit:
        return items, False
    return items[-limit:], True


def _fold_addl_reasoning_into_content(
    content: str | list[str | ContentBlock], additional_kwargs: dict[str, Any]
) -> str | list[str | ContentBlock]:
    """Fold `additional_kwargs["reasoning_content"]` into content as a
    canonical thinking block, so the render loop below never branches on
    provider style.

    Only applies when content does not already carry thinking blocks
    (ReasoningContentChatModel produces canonical blocks — no double-render).
    Non-string / empty / whitespace-only reasoning is silently skipped.
    """
    addl_reasoning = additional_kwargs.get("reasoning_content")
    if not (addl_reasoning and isinstance(addl_reasoning, str) and addl_reasoning.strip()):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "thinking" for b in content):
            return content
        head: ContentBlock = {"type": "thinking", "thinking": addl_reasoning, "index": 0}
        return [head, *content]
    if isinstance(content, str):
        return [
            {"type": "thinking", "thinking": addl_reasoning, "index": 0},
            {"type": "text", "text": content, "index": 1},
        ]
    return content


def _ai_message_items(
    msg: AIMessage, msg_idx: int, next_ts: Callable[[BaseMessage | None], str]
) -> list[TimelineItem]:
    """Split one AIMessage into per-block timeline items.

    Narration (text) and reasoning (thinking) are content blocks, rendered
    at their content position. Code items are rendered from the normalized
    `msg.tool_calls` view rather than from provider-specific content blocks:
    anthropic carries each tool call as a `tool_use` content block, but
    gemini / openai expose tool calls only in `tool_calls` with nothing in
    `content`. Deriving code from `tool_calls` is provider-agnostic and lets
    a single rule produce code items for every provider.

    Each tool call's `block_idx = <number of distinct text/thinking content
    block indices> + <ordinal in tool_calls>`. Because providers emit tool
    calls only after all narration/reasoning (a tool call terminates the
    turn), this offset equals anthropic's `tool_use` content_block_index, so
    anthropic item_ids are unchanged; gemini / openai get well-defined
    non-colliding ids. The streaming side (`agent/graph/_callbacks.py`)
    computes the same offset (over the distinct indices it has streamed) so
    SSE `*_start` ids align with the committed snapshot.

    legacy / no-tools coerce path: when msg.content is a string, treat the
    whole thing as chat with block_idx=0.
    """
    out: list[TimelineItem] = []
    # Reasoning summary attached per thinking block:
    # - reasoning_ms is per-block (each thinking block timed independently by
    #   the llm node, keyed by block_idx) — a model that interleaves several
    #   thinking blocks gets one duration per block, never one turn-spanning
    #   total. `ava_reasoning_ms_by_block` maps str(block_idx) -> ms.
    # - reasoning_tokens stays turn-level (usage_metadata reports one total for
    #   the turn, not per block) and is attached to the first thinking item
    #   only; `reasoning_attached` flips after the first block claims it.
    kwargs = read_ava_kwargs(msg)
    reasoning_ms_by_block = kwargs.get("ava_reasoning_ms_by_block")
    # Legacy turns persisted before per-block timing carried a single
    # turn-level `ava_reasoning_ms` on the first thinking block; read it back so
    # old timelines still show "Thought for X". New turns always write the map.
    legacy_reasoning_ms = kwargs.get("ava_reasoning_ms") if reasoning_ms_by_block is None else None
    code_ms_by_block = kwargs.get("ava_code_ms_by_block")
    from shared.lm.reasoning import extract_reasoning_tokens

    reasoning_tokens = (
        extract_reasoning_tokens(msg.usage_metadata, content=message_content(msg)) or None
    )
    reasoning_attached = False
    # Distinct content-block indices for text/thinking — its size is the offset
    # where tool-call code items begin. A set (not a running count) so a
    # signature-only thinking block sharing its sibling's index does not inflate
    # the offset, matching the streaming side's index set.
    content_indices: set[int] = set()
    # Fold provider-native reasoning (openai `reasoning`) to the canonical
    # `thinking` shape (`shared.lm.reasoning`) on a copy — the stored message stays
    # native for the provider round-trip; only this render reads the normalized
    # view, so the loop below only ever sees text / thinking.
    content = to_canonical_reasoning(message_content(msg))
    # The community ChatMoonshot package carries reasoning in
    # `additional_kwargs["reasoning_content"]` rather than content blocks.
    # Fold it into the normalized content list as a thinking block so the
    # loop below never branches on provider style.
    content = _fold_addl_reasoning_into_content(content, message_addl_kwargs(msg))
    if isinstance(content, str):
        if content:
            out.append(_chat_item(msg_idx, 0, content, next_ts(msg)))
            content_indices.add(0)
    elif isinstance(content, list):
        for pos, block in enumerate(content):
            item, block_idx, reasoning_attached = _content_block_item(
                block,
                pos,
                msg_idx,
                msg,
                next_ts,
                reasoning_ms_by_block,
                legacy_reasoning_ms,
                reasoning_tokens,
                reasoning_attached=reasoning_attached,
            )
            if block_idx is not None:
                content_indices.add(block_idx)
            if item is not None:
                out.append(item)
    else:
        return out

    out.extend(_tool_call_items(msg, msg_idx, content_indices, code_ms_by_block, next_ts))
    return out


def _chat_item(msg_idx: int, block_idx: int, payload: str, created_at: str) -> TimelineItem:
    """A narrated text item (`agent_chat`) — one content block of an AIMessage."""
    return TimelineItem(
        item_id=f"{msg_idx}.{block_idx}",
        kind="agent_chat",
        source=None,
        payload=payload,
        created_at=created_at,
        inbound_id=None,
    )


def _content_block_item(
    block: str | ContentBlock,
    pos: int,
    msg_idx: int,
    msg: AIMessage,
    next_ts: Callable[[BaseMessage | None], str],
    reasoning_ms_by_block: Any,
    legacy_reasoning_ms: Any,
    reasoning_tokens: int | None,
    *,
    reasoning_attached: bool,
) -> tuple[TimelineItem | None, int | None, bool]:
    """Render one text/thinking content block.

    Returns ``(item, block_idx, reasoning_attached)``: ``item`` is None when
    the block renders nothing (signature-only / redacted thinking, empty
    text); ``block_idx`` is the block's index for text/thinking (None for any
    other block type — tool_use / function_call render via `_tool_call_items`
    instead); ``reasoning_attached`` is the caller's flag advanced by one when
    this block claimed the turn-level reasoning_tokens.
    """
    if not isinstance(block, dict):
        return None, None, reasoning_attached
    btype = block.get("type")
    if btype not in ("text", "thinking"):
        # tool_use (anthropic) / function_call (openai) and any other block
        # type: code is rendered from msg.tool_calls below.
        return None, None, reasoning_attached
    block_idx = block.get("index", pos)
    if btype == "thinking":
        # signature_delta block: type=thinking but only carries signature
        # (server verifier), no thinking text — skip render.
        t = block.get("thinking")
        if not (isinstance(t, str) and t):
            return None, block_idx, reasoning_attached
        if reasoning_ms_by_block is not None:
            block_reasoning_ms = reasoning_ms_by_block.get(str(block_idx))
        else:
            # Legacy single value sits on the first thinking block.
            block_reasoning_ms = None if reasoning_attached else legacy_reasoning_ms
        item = TimelineItem(
            item_id=f"{msg_idx}.{block_idx}",
            kind="agent_reasoning",
            source=None,
            payload=t,
            created_at=next_ts(msg),
            inbound_id=None,
            reasoning_ms=block_reasoning_ms,
            reasoning_tokens=None if reasoning_attached else reasoning_tokens,
        )
        return item, block_idx, True
    # text
    t = block.get("text")
    if not (isinstance(t, str) and t):
        return None, block_idx, reasoning_attached
    return _chat_item(msg_idx, block_idx, t, next_ts(msg)), block_idx, reasoning_attached


def _tool_call_items(
    msg: AIMessage,
    msg_idx: int,
    content_indices: set[int],
    code_ms_by_block: Any,
    next_ts: Callable[[BaseMessage | None], str],
) -> list[TimelineItem]:
    """Render each tool call as an `agent_code` item from `msg.tool_calls`.

    Each item's ``block_idx = <number of distinct text/thinking content block
    indices> + <ordinal in tool_calls>`` — the same offset the streaming side
    computes, so SSE ids align with the committed snapshot.
    """
    out: list[TimelineItem] = []
    tool_calls: list[ToolCall] = getattr(msg, "tool_calls", None) or []
    for ordinal, tc in enumerate(tool_calls):
        args = tc.get("args")
        code = ""
        if isinstance(args, dict):
            c = args.get("code")
            if isinstance(c, str):
                code = c
        code_block_idx = len(content_indices) + ordinal
        block_code_elapsed_ms = (
            code_ms_by_block.get(str(code_block_idx)) if code_ms_by_block is not None else None
        )
        out.append(
            TimelineItem(
                item_id=f"{msg_idx}.{code_block_idx}",
                kind="agent_code",
                source=None,
                payload=code,
                created_at=next_ts(msg),
                inbound_id=None,
                sdk_calls=extract_sdk_calls(code) if code else None,
                code_elapsed_ms=block_code_elapsed_ms,
            )
        )
    return out
