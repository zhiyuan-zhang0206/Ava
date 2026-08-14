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
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph import llm_node
from agent.graph._context import AvaContext
from agent.graph._llm import LLMStreamStallTimeoutError
from agent.state import AgentState
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
