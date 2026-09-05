"""shared/lm/_reasoning_compat.py — ReasoningContentChatModel reasoning recovery.

reasoning_content-style providers (MiMo, Kimi, GLM) stream reasoning in the
delta's `reasoning_content` field, which the base ChatOpenAI drops. The subclass
overrides `_convert_chunk_to_generation_chunk` to fold it into a canonical
`{"type":"thinking", ...}` content block. These tests feed synthetic raw
stream-chunk dicts (the shape captured from real MiMo / Kimi astream) — no live
endpoint needed.
"""

from __future__ import annotations

from langchain_core.messages import AIMessageChunk, message_chunk_to_message
from pydantic import SecretStr

from shared.lm._reasoning_compat import ReasoningContentChatModel


def _mimo() -> ReasoningContentChatModel:
    return ReasoningContentChatModel(
        model="mimo-v2.5-pro",
        api_key=SecretStr("sk-test"),
        base_url="https://api.xiaomimimo.com/v1",
        default_headers={"api-key": "sk-test"},
    )


def _raw(delta: dict) -> dict:
    """A raw OpenAI-style stream chunk carrying one delta."""
    return {
        "id": "chatcmpl-x",
        "model": "mimo-v2.5-pro",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


def _convert(m: ReasoningContentChatModel, delta: dict) -> AIMessageChunk:
    gen = m._convert_chunk_to_generation_chunk(_raw(delta), AIMessageChunk, {})  # pyright: ignore[reportUnknownArgumentType]
    assert gen is not None
    msg = gen.message
    assert isinstance(msg, AIMessageChunk)
    return msg


def test_reasoning_content_becomes_thinking_block():
    """A `reasoning_content` delta → a canonical thinking block at index 0."""
    msg = _convert(_mimo(), {"reasoning_content": "Let me think"})
    assert msg.content == [{"type": "thinking", "thinking": "Let me think", "index": 0}]  # pyright: ignore[reportUnknownMemberType]


def test_text_content_becomes_text_block():
    """A plain `content` delta → a text block at index 1 (after reasoning)."""
    msg = _convert(_mimo(), {"content": "The answer is 391"})
    assert msg.content == [{"type": "text", "text": "The answer is 391", "index": 1}]  # pyright: ignore[reportUnknownMemberType]


def test_tool_only_chunk_has_empty_content_and_keeps_tool_chunks():
    """A tool-call delta carries no reasoning/text → empty content list, but the
    tool_call_chunks (code path) survive untouched."""
    m = _mimo()
    delta = {
        "tool_calls": [
            {
                "index": 0,
                "id": "call_0",
                "function": {"name": "execute_code", "arguments": '{"code":"x"}'},
            }
        ]
    }
    msg = _convert(m, delta)
    assert msg.content == []  # pyright: ignore[reportUnknownMemberType]
    assert msg.tool_call_chunks
    assert msg.tool_call_chunks[0]["args"] == '{"code":"x"}'


def test_empty_delta_yields_empty_content():
    """A boundary/role chunk (no reasoning, no text) → empty content list, so
    accumulation stays uniformly list-of-blocks (no str/list mixing)."""
    msg = _convert(_mimo(), {"role": "assistant"})
    assert msg.content == []  # pyright: ignore[reportUnknownMemberType]


def test_accumulation_merges_reasoning_then_text_by_index():
    """Streaming chunks accumulate by index: thinking@0 fragments concat, text@1
    fragments concat, tool args assemble — matching what the timeline/handler
    expect for a list-of-blocks message."""
    m = _mimo()
    chunks = [
        _convert(m, {"role": "assistant"}),
        _convert(m, {"reasoning_content": "17*23 "}),
        _convert(m, {"reasoning_content": "= 391."}),
        _convert(m, {"content": "It is "}),
        _convert(m, {"content": "391."}),
    ]
    acc = chunks[0]
    for c in chunks[1:]:
        acc = acc + c
    final = message_chunk_to_message(acc)
    assert final.content == [  # pyright: ignore[reportUnknownMemberType]
        {"type": "thinking", "thinking": "17*23 = 391.", "index": 0},
        {"type": "text", "text": "It is 391.", "index": 1},
    ]
