"""Message rendering — the textual projection the generation pass reads.

The engine reads text, never console HTML: each message renders to one
deterministic projection, and `render_block_text` assembles a block's section
of a leaf input. Deterministic rendering is what makes a rebuilt tree's
structure reproducible across runs; the render *shape* is calibrated against
the v0.3 demo (task #3704) so the recap-card cost profile matches the measured
anchor.

Message rules (`blocks.py` decides which messages form a block):
- inbound human / tool result: the content as-is;
- AI message: `[thinking]` sections, then text sections, then one
  ``-> tool_call <name>: <args json>`` line per tool call;
- system prompt / session notes / markers / attachments / compact items: not
  rendered (ambient context, not events — the block fold treats them as
  transparent already);
- a rendered body longer than head + tail keeps both ends with an omission
  marker between; the per-message header always carries the message's true
  token size, so nothing silently shrinks.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from shared.hierarchy.blocks import Block
from shared.hierarchy.tokens import count_tokens
from shared.lm.content import content_blocks
from shared.lm.reasoning import to_canonical_reasoning
from shared.message_kwargs import AvaMsgType, message_content, read_ava_kwargs

# Message types that never render: ambient context refreshers and marker rows.
# Compact items additionally never sit inside a block (the fold closes there).
_SKIP_TYPES = frozenset(
    {
        AvaMsgType.SYSTEM_NOTE,
        AvaMsgType.ATTACH,
        AvaMsgType.COMPACT_SUMMARY,
        AvaMsgType.COMPACT_REQUEST,
    }
)


@dataclass(frozen=True)
class RenderParams:
    """Render calibration (defaults = the v0.3 demo shape, task #3704).

    The demo's leaf inputs kept ~6000 characters of an overflowing message's
    opening and ~2800 of its close; the cost anchor (recap cards ~0.7-0.9% of
    daily input tokens) was measured on that shape, so the split is calibration,
    not a bare truncation: the opening carries what the message is about, the
    close carries conclusions and tool-call arguments.
    """

    head_chars: int = 6000
    tail_chars: int = 2800


@dataclass(frozen=True)
class RenderedMessage:
    """One message's rendered projection: truncated body + true size."""

    role: str  # human | ai | tool
    ts: str  # ava_created_at, "" for legacy messages that predate it
    text: str  # the truncated body
    tokens: int  # true (untruncated) body token count


def _role(msg: BaseMessage) -> str | None:
    """The render role, or None when the message does not render."""
    if isinstance(msg, SystemMessage):
        return None
    if read_ava_kwargs(msg).get("ava_msg_type") in _SKIP_TYPES:
        return None
    if isinstance(msg, AIMessage):
        return "ai"
    if isinstance(msg, HumanMessage):
        return "human"
    if isinstance(msg, ToolMessage):
        return "tool"
    return None


def _content_text(content: object) -> str:
    """Flatten a message content value to its text (string or text blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        items = content_blocks(cast("list[str | dict[str, Any]]", content))
        for item in items:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _ai_body(msg: AIMessage) -> str:
    """An AI message's body: thinking sections, text sections, tool calls."""
    parts: list[str] = []
    content = to_canonical_reasoning(message_content(msg))
    if isinstance(content, str):
        if content.strip():
            parts.append(content)
    elif isinstance(content, list):
        # `to_canonical_reasoning` retypes the list branch less than
        # `content_blocks` needs; the runtime isinstance guard below is the real
        # check (blocks are dicts, bare strings pass through).
        for raw in content_blocks(cast("list[str | dict[str, Any]]", content)):
            if not isinstance(raw, dict):
                continue
            block = cast("dict[str, Any]", raw)
            if block.get("type") == "thinking":
                thinking = block.get("thinking")
                if isinstance(thinking, str) and thinking.strip():
                    parts.append(f"[thinking] {thinking}")
            elif block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
    for tool_call in msg.tool_calls:
        name = tool_call.get("name") or "?"
        args = tool_call.get("args")
        rendered_args = (
            json.dumps(args, ensure_ascii=False, default=str) if isinstance(args, dict) else ""
        )
        parts.append(f"-> tool_call {name}: {rendered_args}")
    return "\n\n".join(parts)


def _truncate(body: str, params: RenderParams) -> str:
    """Keep both ends of an oversized body with an omission marker between."""
    if len(body) <= params.head_chars + params.tail_chars:
        return body
    omitted = len(body) - params.head_chars - params.tail_chars
    return (
        body[: params.head_chars]
        + f"\n\u2026[omitted {omitted} chars]\u2026\n"
        + body[-params.tail_chars :]
    )


def render_message(msg: BaseMessage, params: RenderParams | None = None) -> RenderedMessage | None:
    """Render one message; None when the message does not render."""
    role = _role(msg)
    if role is None:
        return None
    p = params or RenderParams()
    if isinstance(msg, AIMessage):
        body = _ai_body(msg)
    else:
        body = _content_text(message_content(msg))
        if isinstance(msg, ToolMessage):
            exit_code = read_ava_kwargs(msg).get("ava_exit_code")
            if exit_code is not None:
                body = f"[exit={exit_code}]\n{body}"
    if not body.strip():
        return None
    ts = read_ava_kwargs(msg).get("ava_created_at")
    # Legacy messages predate ava_created_at; the render carries "" rather than
    # a synthetic time (spans are informational here; the console's synthetic
    # anchor logic needs inbound rows this layer deliberately does not read).
    return RenderedMessage(
        role=role,
        ts=ts if isinstance(ts, str) else "",
        text=_truncate(body, p),
        tokens=count_tokens(body),
    )


@dataclass(frozen=True)
class RenderedBlock:
    """One block's assembled render: text, true input size, and its end times."""

    text: str
    tokens: int  # count_tokens(text) — the block's source size as the LLM reads it
    t0: str
    t1: str


def render_block(
    msgs: Sequence[BaseMessage], block: Block, params: RenderParams | None = None
) -> RenderedBlock:
    """Assemble one block's input section: a heading plus message sections.

    The heading's `~N tokens` is the sum of the messages' true sizes (an
    orientation hint); the returned `tokens` is the assembled text's exact
    count — the block's source size for the seal/unit accounting.
    """
    p = params or RenderParams()
    rendered = [
        (idx, r)
        for idx in range(block.i0, block.i1 + 1)
        if (r := render_message(msgs[idx], p)) is not None
    ]
    if not rendered:
        return RenderedBlock(text="", tokens=0, t0="", t1="")
    tok_total = sum(r.tokens for _idx, r in rendered)
    t0 = rendered[0][1].ts
    t1 = rendered[-1][1].ts
    lines = [
        f"### block {block.i0} (messages i{block.i0}-i{block.i1}, {t0} -> {t1}, ~{tok_total} tokens)",
        "",
    ]
    for idx, r in rendered:
        lines.append(f"**[i{idx} | {r.role} | {r.ts} | {r.tokens} tok]**")
        lines.append(r.text)
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    return RenderedBlock(text=text, tokens=count_tokens(text), t0=t0, t1=t1)


def render_block_text(
    msgs: Sequence[BaseMessage], block: Block, params: RenderParams | None = None
) -> str:
    """One block's rendered source text (`render_block().text`)."""
    return render_block(msgs, block, params).text


def block_source_tokens(
    msgs: Sequence[BaseMessage], block: Block, params: RenderParams | None = None
) -> int:
    """The block's source size in tokens — what `Unit.tok` carries for blocks."""
    return render_block(msgs, block, params).tokens
