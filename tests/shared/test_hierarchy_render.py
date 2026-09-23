"""Unit tests for the hierarchy message rendering (`shared/agents/history/hierarchy/render.py`).

Contracts locked here: the projection is deterministic and content-faithful
(thinking / text / tool calls / exits / inbound texts all reach the input); the
non-event message families never render; an oversized body keeps head + tail
with an omission marker while its header still reports the true size; block
assembly carries per-message headers in stream order.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolCall, ToolMessage

from shared.agents.history.hierarchy.blocks import Block
from shared.agents.history.hierarchy.render import (
    RenderParams,
    block_source_tokens,
    render_block_text,
    render_message,
)
from shared.agents.history.hierarchy.tokens import count_tokens

TS = "2026-09-12T12:03:27+08:00"


def inbound(text: str, ts: str = TS) -> HumanMessage:
    return HumanMessage(
        content=text,
        additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": ts},
    )


def note(text: str) -> HumanMessage:
    return HumanMessage(
        content=text,
        additional_kwargs={"ava_msg_type": "system_note", "ava_created_at": TS},
    )


def ai(
    content: str | list[str | dict[str, Any]], tool_calls: list[ToolCall] | None = None
) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=tool_calls or [],
        additional_kwargs={"ava_created_at": TS},
    )


def exec_out(text: str, exit_code: int | None = 0) -> ToolMessage:
    kwargs: dict[str, object] = {"ava_msg_type": "exec_output", "ava_created_at": TS}
    if exit_code is not None:
        kwargs["ava_exit_code"] = exit_code
    return ToolMessage(content=text, tool_call_id="tc1", additional_kwargs=kwargs)


# ---- message-level rendering ----


def test_inbound_renders_content_as_is() -> None:
    r = render_message(inbound("Agent 5 [ts]:\n\nhello there"))
    assert r is not None
    assert r.role == "human"
    assert r.ts == TS
    assert r.text == "Agent 5 [ts]:\n\nhello there"
    assert r.tokens == count_tokens(r.text)


def test_ai_renders_thinking_text_and_tool_calls_in_order() -> None:
    r = render_message(
        ai(
            [
                {"type": "thinking", "thinking": "weigh the options"},
                {"type": "text", "text": "going with B"},
            ],
            tool_calls=[{"name": "execute_code", "args": {"code": "print(1)"}, "id": "t1"}],
        )
    )
    assert r is not None
    assert r.role == "ai"
    assert r.text == (
        "[thinking] weigh the options\n\n"
        'going with B\n\n-> tool_call execute_code: {"code": "print(1)"}'
    )


def test_ai_with_only_tool_calls_still_renders() -> None:
    r = render_message(ai("", tool_calls=[{"name": "execute_code", "args": {}, "id": "t1"}]))
    assert r is not None
    assert r.text == "-> tool_call execute_code: {}"


def test_exec_output_renders_with_exit_code() -> None:
    r = render_message(exec_out("Code execution output [ts]:\nresult ok", exit_code=0))
    assert r is not None
    assert r.role == "tool"
    assert r.text == "[exit=0]\nCode execution output [ts]:\nresult ok"
    r2 = render_message(exec_out("boom", exit_code=None))
    assert r2 is not None
    assert not r2.text.startswith("[exit=")


def test_context_messages_do_not_render() -> None:
    assert render_message(SystemMessage(content="system prompt")) is None
    assert render_message(note("[system] background refresh")) is None
    assert render_message(HumanMessage(content="  ")) is None
    compact = HumanMessage(
        content="compact summary",
        additional_kwargs={"ava_msg_type": "compact_summary", "ava_created_at": TS},
    )
    assert render_message(compact) is None
    attach = HumanMessage(
        content=[{"type": "text", "text": "see image"}, {"type": "image", "url": "u"}],
        additional_kwargs={"ava_msg_type": "attach", "ava_created_at": TS},
    )
    assert render_message(attach) is None


# ---- truncation ----


def test_oversized_body_keeps_head_and_tail_with_marker() -> None:
    body = "A" * 7000 + "B" * 3000
    r = render_message(inbound(body))
    assert r is not None
    assert r.text.startswith("A" * 6000)
    assert "\u2026[omitted 1200 chars]\u2026" in r.text
    assert r.text.endswith("B" * 2800)
    # The header size is the TRUE body size, not the truncated one.
    assert r.tokens == count_tokens(body)
    assert count_tokens(r.text) < r.tokens


def test_body_within_window_is_untouched() -> None:
    body = "C" * 8000
    r = render_message(inbound(body))
    assert r is not None
    assert r.text == body


def test_truncation_window_is_param_driven() -> None:
    r = render_message(inbound("D" * 500), RenderParams(head_chars=100, tail_chars=50))
    assert r is not None
    assert r.text.startswith("D" * 100)
    assert "omitted 350 chars" in r.text
    assert r.text.endswith("D" * 50)


# ---- block assembly ----


def test_block_assembly_carries_headers_in_stream_order() -> None:
    msgs = [
        inbound("Agent 5 [ts]:\n\nplease check"),
        note("[system] ambient refresh"),
        ai(
            [{"type": "thinking", "thinking": "checking"}],
            tool_calls=[{"name": "execute_code", "args": {"code": "x"}, "id": "t1"}],
        ),
        exec_out("Code execution output [ts]:\nall good", exit_code=0),
    ]
    block = Block(i0=0, i1=3, kind="ai")
    text = render_block_text(msgs, block)
    assert text.startswith("### block 0 (messages i0-i3, ")
    assert text.count("**[i") == 3  # the system note does not render
    assert "**[i0 | human | " in text
    assert "**[i2 | ai | " in text
    assert "**[i3 | tool | " in text
    assert "[exit=0]" in text
    assert "please check" in text and "all good" in text
    assert "ambient refresh" not in text


def test_block_source_tokens_matches_rendered_text() -> None:
    msgs = [inbound("hello"), ai("world")]
    block = Block(i0=0, i1=1, kind="ai")
    assert block_source_tokens(msgs, block) == count_tokens(render_block_text(msgs, block))


def test_block_of_only_context_messages_renders_empty() -> None:
    msgs = [note("[system] a"), note("[system] b")]
    assert render_block_text(msgs, Block(i0=0, i1=1, kind="ai")) == ""
