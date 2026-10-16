from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, cast

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCall,
)

from base.agents.history.timeline_inputs import TimelineReadInputs
from base.agents.history.timeline_item import TimelineItem as TimelineItem
from base.agents.impersonation.history import ImpersonationMetadata
from base.agents.messages.kwargs import (
    AvaMessageKwargs,
    AvaMsgType,
    NoteTag,
    kwargs_read_time,
    read_ava_kwargs,
)
from base.agents.sdk.telemetry import SdkCall, sdk_calls_by_tool_call_id
from base.lm.content import message_addl_kwargs, message_content

__all__ = [
    "build_timeline_items",
    "tail_window",
    "timeline_default_limit",
]


# ava:ref base/agents/history/docs/timeline/note-timestamp-policy.ava.okf.md#note-timestamp-policy
_NO_TIMESTAMP_NOTE_TAGS = frozenset(
    {
        NoteTag.MEMORY,
        NoteTag.SKILL_RECALL,
        NoteTag.AGENT_ID,
        NoteTag.COMPACT_REMINDER,
        NoteTag.HISTORY_DUMP,
        NoteTag.SDK_HINT,
        NoteTag.MEMORY_WRITE_CHECK,
        NoteTag.TO_USER_SOFT_LIMIT,
        NoteTag.AGENT_REPLY,
        NoteTag.SILENT_IDLE_CONTINUE,
        NoteTag.SECURITY,
        NoteTag.HEARTBEAT_PAUSE,
        NoteTag.CONTEXT,
        NoteTag.PROJECT_SKILLS,
        NoteTag.PRELOADED_SKILLS,
        NoteTag.AGENT_MEMORY,
        NoteTag.INHERITED_MEMORY,
        NoteTag.EXEC_TIMEOUT,
        NoteTag.TIMEZONE,
    }
)


def _inbound_text(content: str | list[str | dict[str, Any]]) -> str:
    from base.lm.reasoning import content_blocks

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


# ava:ref base/agents/history/docs/timeline/timeline.ava.okf.md#timeline-window-contract
def build_timeline_items(
    messages: Sequence[BaseMessage],
    *,
    inputs: TimelineReadInputs,
    start: int = 0,
    segment_prefix: str = "",
) -> tuple[list[TimelineItem], int]:
    items: list[TimelineItem] = []
    sdk_by_id = sdk_calls_by_tool_call_id(messages, start)
    msg_count = len(messages)
    for msg_idx, msg in enumerate(messages[start:], start=start):
        if isinstance(msg, SystemMessage):
            raw_content = message_content(msg)
            items.append(
                _system_prompt_item(
                    msg_idx, raw_content if isinstance(raw_content, str) else str(raw_content)
                )
            )
            continue
        raw_content = message_content(msg)
        kwargs = read_ava_kwargs(msg)
        if kwargs.get("ava_msg_type") == AvaMsgType.INBOUND:
            items.append(_inbound_item(msg_idx, raw_content, kwargs))
        else:
            items.extend(_message_items(msg, msg_idx, kwargs, sdk_by_id, inputs))
    _add_source_coordinates(items, messages)
    if segment_prefix:
        items = [
            item.model_copy(update={"item_id": f"{segment_prefix}.{item.item_id}"})
            for item in items
        ]
    return items, msg_count


def _add_source_coordinates(items: list[TimelineItem], messages: Sequence[BaseMessage]) -> None:
    for item in items:
        msg_idx, block_idx = (int(part) for part in item.item_id.split("."))
        message = messages[msg_idx]
        kwargs = read_ava_kwargs(message)
        if message.id and not kwargs.get("ava_ephemeral_message_id"):
            item.source_message_id = message.id
            item.source_block_idx = block_idx
        embedded_id = kwargs.get("ava_inbound_id")
        if isinstance(embedded_id, int) and not isinstance(embedded_id, bool) and embedded_id > 0:
            item.source_inbound_id = embedded_id
            item.source_block_idx = block_idx


def _message_items(
    msg: BaseMessage,
    msg_idx: int,
    kwargs: AvaMessageKwargs,
    sdk_by_id: dict[str, Any],
    inputs: TimelineReadInputs,
) -> list[TimelineItem]:
    raw_content = message_content(msg)
    content = raw_content if isinstance(raw_content, str) else str(raw_content)
    ava_type = kwargs.get("ava_msg_type")
    created_at = kwargs_read_time(kwargs)
    if ava_type == AvaMsgType.ATTACH:
        return [_attach_item(msg_idx, raw_content, created_at)]
    if ava_type == AvaMsgType.EXEC_OUTPUT:
        return [_exec_output_item(msg_idx, content, created_at, kwargs.get("ava_exec_ms"))]
    if ava_type == AvaMsgType.SYSTEM_NOTE:
        item = _system_note_item(msg_idx, content, kwargs.get("ava_note_tag"), created_at)
        if "ava_impersonation" in kwargs:
            item.impersonation = ImpersonationMetadata.model_validate(kwargs["ava_impersonation"])
        return [item]
    if ava_type == AvaMsgType.COMPACT_SUMMARY:
        return [
            _compact_item(
                msg_idx,
                "inbound_compact_summary",
                content,
                created_at,
                kwargs.get("ava_compact_id"),
                inputs=inputs,
            )
        ]
    if ava_type == AvaMsgType.COMPACT_REQUEST:
        return [
            _compact_item(
                msg_idx,
                "inbound_compact_request",
                content,
                created_at,
                kwargs.get("ava_compact_id"),
                inputs=inputs,
            )
        ]
    if isinstance(msg, AIMessage):
        return list(ai_message_items(msg, msg_idx, sdk_by_id))
    if isinstance(msg, HumanMessage):
        return [_fallback_human_item(msg_idx, content, created_at)]
    return []


def _system_prompt_item(msg_idx: int, content: str) -> TimelineItem:
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
) -> TimelineItem:
    embedded_id = None
    if "ava_inbound_id" in kwargs:
        embedded_id = kwargs["ava_inbound_id"]
        if type(embedded_id) is not int or embedded_id <= 0:
            raise ValueError(f"inbound ava_inbound_id must be a positive int, got {embedded_id!r}")
    images = kwargs.get("ava_image_urls")
    return TimelineItem(
        item_id=f"{msg_idx}.0",
        kind="inbound_chat",
        source=kwargs.get("ava_source"),
        payload=_inbound_text(raw_content),
        created_at=kwargs_read_time(kwargs),
        inbound_id=embedded_id,
        images=images if images else None,
    )


def _attach_images(content: str | list[str | dict[str, Any]]) -> list[str] | None:
    from base.lm.reasoning import content_blocks

    if not isinstance(content, list):
        return None
    urls: list[str] = []
    for block in content_blocks(content):
        if not isinstance(block, dict) or block.get("type") != "image_url":
            continue
        image_url = cast("dict[str, Any] | None", block.get("image_url"))
        if image_url is None:
            continue
        url = image_url.get("url")
        if isinstance(url, str) and url.startswith("data:"):
            urls.append(url)
    return urls or None


# ava:ref base/agents/history/docs/timeline/timeline.ava.okf.md#attach-caption-pairing
def _attach_image_captions(content: str | list[str | dict[str, Any]]) -> list[str] | None:
    from base.lm.reasoning import content_blocks

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
    msg_idx: int, raw_content: str | list[str | dict[str, Any]], created_at: str | None
) -> TimelineItem:
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


def _exec_output_item(
    msg_idx: int, content: str, created_at: str | None, exec_ms: Any
) -> TimelineItem:
    return TimelineItem(
        item_id=f"{msg_idx}.0",
        kind="code_output",
        source=None,
        payload=content,
        created_at=created_at,
        inbound_id=None,
        exec_ms=exec_ms,
    )


def _system_note_item(
    msg_idx: int, content: str, note_tag: Any, created_at: str | None
) -> TimelineItem:
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
    created_at: str | None,
    compact_id: str | None = None,
    *,
    inputs: TimelineReadInputs,
) -> TimelineItem:
    ts = f" {inputs.clock_factory().now_timestamp()}" if inputs.timestamps_enabled() else ""
    label = "Compact summary" if kind == "inbound_compact_summary" else "Compact request"
    return TimelineItem(
        item_id=f"{msg_idx}.0",
        kind=kind,
        source=None,
        payload=f"{label}{ts}:\n\n{content}",
        created_at=created_at,
        inbound_id=None,
        compact_id=compact_id,
    )


# ava:ref base/agents/history/docs/timeline/timeline.ava.okf.md#human-fallback-fail-loud
def _fallback_human_item(msg_idx: int, content: str, created_at: str | None) -> TimelineItem:
    return TimelineItem(
        item_id=f"{msg_idx}.0",
        kind="system_marker",
        source=None,
        payload=content,
        created_at=created_at,
        inbound_id=None,
    )


# ava:ref base/agents/history/docs/timeline/timeline.ava.okf.md#timeline-window-unit
def timeline_default_limit(*, limit_reader: Callable[[], int]) -> int:
    return limit_reader()


def tail_window(items: list[TimelineItem], limit: int) -> tuple[list[TimelineItem], bool]:
    if limit <= 0 or len(items) <= limit:
        return items, False
    return items[-limit:], True


def _fold_addl_reasoning_into_content(
    content: str | list[Any], additional_kwargs: dict[str, Any]
) -> str | list[Any]:
    from base.lm.reasoning import ContentBlock

    addl_reasoning = additional_kwargs.get("reasoning_content")
    if not (addl_reasoning and isinstance(addl_reasoning, str) and addl_reasoning.strip()):
        return content
    if isinstance(content, list):
        typed_content = cast("list[str | ContentBlock]", content)
        if any(isinstance(b, dict) and b.get("type") == "thinking" for b in typed_content):
            return content
        head: ContentBlock = {"type": "thinking", "thinking": addl_reasoning, "index": 0}
        return [head, *typed_content]
    if isinstance(content, str):
        return [
            {"type": "thinking", "thinking": addl_reasoning, "index": 0},
            {"type": "text", "text": content, "index": 1},
        ]
    return content


def ai_message_items(
    msg: AIMessage,
    msg_idx: int,
    sdk_by_id: Mapping[str, list[SdkCall]],
) -> list[TimelineItem]:
    out: list[TimelineItem] = []
    kwargs = read_ava_kwargs(msg)
    created_at = kwargs_read_time(kwargs)
    reasoning_ms_by_block = kwargs.get("ava_reasoning_ms_by_block")
    code_ms_by_block = kwargs.get("ava_code_ms_by_block")
    from base.lm.reasoning import extract_reasoning_tokens, to_canonical_reasoning

    reasoning_tokens = (
        extract_reasoning_tokens(msg.usage_metadata, content=message_content(msg)) or None
    )
    reasoning_attached = False
    content_indices: set[int] = set()
    content = to_canonical_reasoning(message_content(msg))
    content = _fold_addl_reasoning_into_content(content, message_addl_kwargs(msg))
    if isinstance(content, str):
        if content:
            out.append(_chat_item(msg_idx, 0, content, created_at))
            content_indices.add(0)
    elif isinstance(content, list):
        for pos, block in enumerate(content):
            item, block_idx, reasoning_attached = _content_block_item(
                block,
                pos,
                msg_idx,
                created_at,
                reasoning_ms_by_block,
                reasoning_tokens,
                reasoning_attached=reasoning_attached,
            )
            if block_idx is not None:
                content_indices.add(block_idx)
            if item is not None:
                out.append(item)
    else:
        return out

    out.extend(
        _tool_call_items(msg, msg_idx, content_indices, code_ms_by_block, created_at, sdk_by_id)
    )
    return out


def _chat_item(msg_idx: int, block_idx: int, payload: str, created_at: str | None) -> TimelineItem:
    return TimelineItem(
        item_id=f"{msg_idx}.{block_idx}",
        kind="agent_chat",
        source=None,
        payload=payload,
        created_at=created_at,
        inbound_id=None,
    )


# ava:ref base/agents/history/docs/timeline/timeline.ava.okf.md#reasoning-timing
def _content_block_item(
    block: str | dict[str, Any],
    pos: int,
    msg_idx: int,
    created_at: str | None,
    reasoning_ms_by_block: Any,
    reasoning_tokens: int | None,
    *,
    reasoning_attached: bool,
) -> tuple[TimelineItem | None, int | None, bool]:
    from base.lm.reasoning import ContentBlock

    if not isinstance(block, dict):
        return None, None, reasoning_attached
    if (typed_block := cast(ContentBlock, block)).get("type") not in ("text", "thinking"):
        return None, None, reasoning_attached
    block_idx = typed_block.get("index", pos)
    if typed_block.get("type") == "thinking":
        t = typed_block.get("thinking")
        if not (isinstance(t, str) and t):
            return None, block_idx, reasoning_attached
        block_reasoning_ms = (
            None if reasoning_ms_by_block is None else reasoning_ms_by_block.get(str(block_idx))
        )
        item = TimelineItem(
            item_id=f"{msg_idx}.{block_idx}",
            kind="agent_reasoning",
            source=None,
            payload=t,
            created_at=created_at,
            inbound_id=None,
            reasoning_ms=block_reasoning_ms,
            reasoning_tokens=None if reasoning_attached else reasoning_tokens,
        )
        return item, block_idx, True
    t = typed_block.get("text")
    if not (isinstance(t, str) and t):
        return None, block_idx, reasoning_attached
    return _chat_item(msg_idx, block_idx, t, created_at), block_idx, reasoning_attached


# ava:ref base/agents/history/docs/timeline/timeline.ava.okf.md#tool-call-block-offset
def _tool_call_items(
    msg: AIMessage,
    msg_idx: int,
    content_indices: set[int],
    code_ms_by_block: Any,
    created_at: str | None,
    sdk_by_id: Mapping[str, list[SdkCall]],
) -> list[TimelineItem]:
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
                created_at=created_at,
                inbound_id=None,
                sdk_calls=sdk_by_id.get(tc.get("id") or ""),
                code_elapsed_ms=block_code_elapsed_ms,
            )
        )
    return out
