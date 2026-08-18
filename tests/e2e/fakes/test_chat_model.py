"""ScriptedFakeChatModel self unit test — cursor / chunk encoding / exhaustion.

A silently broken fake = silently green e2e, so the fake's own non-trivial logic
(cursor advancement / tool_call_chunks JSON encoding / IndexError out-of-bounds)
must have unit coverage.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, message_chunk_to_message

from tests.e2e.fakes._chat_model import ScriptedFakeChatModel, ScriptExhaustedError


def _msg(content: str = "", *, tool_call: dict | None = None) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=[tool_call] if tool_call else [],
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


async def test_cursor_advances_per_astream_call() -> None:
    fake = ScriptedFakeChatModel(script=(_msg("hi"), _msg("bye")))
    assert fake.cursor == 0

    chunks_a = [c async for c in fake._astream(messages=[])]
    assert fake.cursor == 1
    assert len(chunks_a) == 1
    # langchain stub — message.content has Unknown slots.
    assert chunks_a[0].message.content == "hi"  # pyright: ignore[reportUnknownMemberType]

    chunks_b = [c async for c in fake._astream(messages=[])]
    assert fake.cursor == 2
    assert chunks_b[0].message.content == "bye"  # pyright: ignore[reportUnknownMemberType]


async def test_script_exhausted_raises_distinct_error() -> None:
    fake = ScriptedFakeChatModel(script=(_msg("only"),))
    [_ async for _ in fake._astream(messages=[])]

    with pytest.raises(ScriptExhaustedError, match="exhausted at turn 1"):
        [_ async for _ in fake._astream(messages=[])]


async def test_tool_call_chunk_args_serialized_to_json() -> None:
    """LangChain ToolCallChunk `args` must be a JSON string — the upper layer
    `message_chunk_to_message` will parse it back to a dict. Verify encoding + roundtrip."""
    tool_call = {
        "id": "call_1",
        "name": "execute_code",
        "args": {"code": "print('中文 + quotes')"},
    }
    fake = ScriptedFakeChatModel(script=(_msg(tool_call=tool_call),))

    chunks = [c async for c in fake._astream(messages=[])]
    chunk_msg = chunks[0].message
    assert isinstance(chunk_msg, AIMessageChunk)
    assert len(chunk_msg.tool_call_chunks) == 1
    args_str = chunk_msg.tool_call_chunks[0]["args"]
    assert args_str is not None
    assert json.loads(args_str) == {"code": "print('中文 + quotes')"}

    # simulate upper-layer accumulation + finalize: chunk → AIMessage parses args back
    final_msg = message_chunk_to_message(chunk_msg)
    assert isinstance(final_msg, AIMessage)
    assert final_msg.tool_calls[0]["args"] == {"code": "print('中文 + quotes')"}


def test_bind_tools_returns_self_unchanged() -> None:
    fake = ScriptedFakeChatModel(script=(_msg("x"),))
    bound = fake.bind_tools(tools=[object()])
    assert bound is fake


async def test_bind_tools_shares_cursor_with_unbound() -> None:
    """bind_tools returns self, not a new instance — bound shares cursor with
    original fake; calling either advances the same counter. In production the caller
    uses bound_llm, and cursor advancement hits fake.cursor.
    """
    fake = ScriptedFakeChatModel(script=(_msg("a"), _msg("b")))
    bound = fake.bind_tools(tools=[])

    [_ async for _ in bound._astream(messages=[])]
    assert fake.cursor == 1  # bound call advanced the original fake's cursor

    [_ async for _ in fake._astream(messages=[])]
    assert fake.cursor == 2
