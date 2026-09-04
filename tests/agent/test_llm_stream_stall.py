"""LLM stream inter-chunk stall timeout — abort turn when the server hangs to prevent
indefinite blocking.

161 / 165 incident: when DeepSeek service degraded, the stream started but never
returned another chunk; the default anthropic SDK 600s overall timeout was the only
safety net, leaving the agent silently stuck for 4-10 minutes; turn_end ok=False never
even fired, so the frontend saw neither a stream nor an error.

Fix: wrap `__anext__` on `_stream` with `asyncio.wait_for`,
`settings.lm.llm_stream_ttft_timeout_seconds` and `settings.lm.llm_stream_inter_chunk_timeout_seconds`
raise `LLMStreamStallTimeoutError`. Together with PR #60's finally `logger.opt(exception=
True).warning`, events.payload automatically carries traceback / exception_type.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph import llm_node
from agent.graph._context import AvaContext
from agent.graph._llm import LLMStreamStallTimeoutError
from agent.graph._llm_stream import _consume_llm, _consume_stream_with_stall_timeout
from agent.state import AgentState
from shared.config import settings
from tests.agent._fakes import make_fake_ops_pool

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}


def _make_runtime(llm: MagicMock) -> Runtime[AvaContext]:
    """Same pattern as test_cancel.py: fake llm returns itself via bind_tools (chain method)."""
    llm.bind_tools.return_value = llm
    ctx = AvaContext(
        ops_pool=make_fake_ops_pool(),
        llm=llm,
        event_publisher=MagicMock(),
    )
    return Runtime(context=ctx)


async def test_stall_at_ttft_raises_with_ttft_marker(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server completely unresponsive (zero bytes emitted) → raise LLMStreamStallTimeoutError containing 'TTFT'."""

    async def _hang_immediately() -> AsyncIterator[AIMessageChunk]:
        await asyncio.Future()  # never returns — simulates server hang
        yield  # type: ignore[unreachable]

    monkeypatch.setattr("shared.config.settings.lm.llm_stream_ttft_timeout_seconds", 0.05)
    monkeypatch.setattr(
        "shared.config.settings.lm.llm_non_streaming_fallback_timeout_seconds", 0.05
    )
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _hang_immediately()

    # Non-streaming fallback: set ainvoke to also raise LLMStreamStallTimeoutError
    # so the original test assertion (stall → error) still holds.
    async def _ainvoke_also_fails(*args, **kwargs):
        raise LLMStreamStallTimeoutError("fallback also stalled")

    fake_llm.ainvoke = _ainvoke_also_fails
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with pytest.raises(LLMStreamStallTimeoutError, match="fallback also stalled"):
        await llm_node(state, _make_runtime(fake_llm), _CONFIG)


async def test_stall_mid_stream_raises_with_chunk_count(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emits N chunks then hangs → raise LLMStreamStallTimeoutError containing
    'mid-stream after N chunks'. Triage split: TTFT = server never connected,
    mid-stream = connected then died, ops handles them differently."""

    async def _stream_then_hang() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content="hello ")
        yield AIMessageChunk(content="world")
        yield AIMessageChunk(content="!")
        await asyncio.Future()  # hangs after 3 chunks

    monkeypatch.setattr("shared.config.settings.lm.llm_stream_ttft_timeout_seconds", 10.0)
    monkeypatch.setattr("shared.config.settings.lm.llm_stream_inter_chunk_timeout_seconds", 0.05)
    monkeypatch.setattr(
        "shared.config.settings.lm.llm_non_streaming_fallback_timeout_seconds", 0.05
    )
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _stream_then_hang()

    # Non-streaming fallback: set ainvoke to also raise LLMStreamStallTimeoutError
    # so the original test assertion (stall → error) still holds.
    async def _ainvoke_also_fails(*args, **kwargs):
        raise LLMStreamStallTimeoutError("fallback also stalled")

    fake_llm.ainvoke = _ainvoke_also_fails
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with pytest.raises(LLMStreamStallTimeoutError, match="fallback also stalled"):
        await llm_node(state, _make_runtime(fake_llm), _CONFIG)


async def test_normal_stream_completes_no_stall_timeout(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal stream completes within timeout → no raise, llm_node finishes and
    returns Command. Locks the "chunk interval < timeout" path against accidental regression."""

    async def _normal_stream() -> AsyncIterator[AIMessageChunk]:
        # Last chunk MUST carry usage_metadata (agent #113 incident: fail-fast assert
        # final_msg.usage_metadata is not None) + response_metadata.stop_reason
        # (agent #169 incident: without it _validate_stop_reason raises
        # LLMStreamCorruptedError to prevent silent idle fallthrough).
        yield AIMessageChunk(
            content="ok",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    # 1.0s timeout is generous for a normal stream (zero latency), but it is a test value, not the production default
    monkeypatch.setattr("shared.config.settings.lm.llm_stream_ttft_timeout_seconds", 1.0)
    monkeypatch.setattr("shared.config.settings.lm.llm_stream_inter_chunk_timeout_seconds", 1.0)
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _normal_stream()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    # No raise — normal stream completed. The specific return value is handled by
    # llm_node's existing path (BEFORE_EXEC); this test only locks "not falsely
    # killed by stall timeout"
    result = await llm_node(state, _make_runtime(fake_llm), _CONFIG)
    assert result is not None


async def test_total_timeout_falls_back_while_chunks_keep_arriving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drip-fed stream cannot evade the per-attempt total-duration ceiling."""
    import agent.graph._llm_stream as stream_module

    clock = [0.0]
    fallback_called = False
    streamed_chunks = 0

    def fake_monotonic() -> float:
        return clock[0]

    async def _dripping_stream() -> AsyncIterator[AIMessageChunk]:
        nonlocal streamed_chunks
        while not fallback_called:
            streamed_chunks += 1
            if streamed_chunks > 20:
                pytest.fail("stream total timeout never stopped the condition-driven fake")
            clock[0] += 0.4
            yield AIMessageChunk(content="drip")

    async def _fallback(*args: object, **kwargs: object) -> AIMessage:
        nonlocal fallback_called
        fallback_called = True
        return AIMessage(content="fallback")

    monkeypatch.setattr(stream_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(settings.lm, "llm_stream_total_timeout_seconds", 1.0)
    monkeypatch.setattr(settings.lm, "llm_stream_ttft_timeout_seconds", 10.0)
    monkeypatch.setattr(settings.lm, "llm_stream_inter_chunk_timeout_seconds", 10.0)
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _dripping_stream()
    fake_llm.ainvoke = _fallback
    chunks: list[AIMessageChunk] = []

    await _consume_llm(fake_llm, [], chunks=chunks, handler=MagicMock())

    assert fallback_called
    assert streamed_chunks <= 20
    assert [cast(str, chunk.content) for chunk in chunks] == [  # pyright: ignore[reportUnknownMemberType]
        "fallback"
    ]


async def test_stream_below_total_timeout_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.graph._llm_stream as stream_module

    clock = [0.0]

    async def _stream() -> AsyncIterator[AIMessageChunk]:
        for content in ("a", "b"):
            clock[0] += 0.4
            yield AIMessageChunk(content=content)

    monkeypatch.setattr(stream_module.time, "monotonic", lambda: clock[0])
    chunks: list[AIMessageChunk] = []

    await _consume_stream_with_stall_timeout(
        _stream(),
        chunks=chunks,
        handler=MagicMock(),
        ttft_timeout=10.0,
        inter_chunk_timeout=10.0,
        total_timeout=1.0,
    )

    assert [cast(str, chunk.content) for chunk in chunks] == [  # pyright: ignore[reportUnknownMemberType]
        "a",
        "b",
    ]


async def test_none_disables_stream_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper's None contract permits an intentionally unbounded caller."""
    import agent.graph._llm_stream as stream_module

    clock = [0.0]

    async def _long_stream() -> AsyncIterator[AIMessageChunk]:
        while clock[0] <= 3600.0:
            clock[0] += 1000.0
            yield AIMessageChunk(content="still alive")

    monkeypatch.setattr(stream_module.time, "monotonic", lambda: clock[0])
    chunks: list[AIMessageChunk] = []

    await _consume_stream_with_stall_timeout(
        _long_stream(),
        chunks=chunks,
        handler=MagicMock(),
        ttft_timeout=10.0,
        inter_chunk_timeout=10.0,
        total_timeout=None,
    )

    assert clock[0] > 3600.0
    assert len(chunks) == 4
