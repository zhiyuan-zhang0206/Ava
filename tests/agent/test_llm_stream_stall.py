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
import time
from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import ExecutionInfo, Runtime

from agent.graph import llm_node
from agent.graph._context import AvaContext
from agent.graph._llm import (
    LLMRetryBudgetExceededError,
    LLMStreamStallPairError,
)
from agent.graph._llm_errors import _record_stall_pair_streak, _reset_stall_pair_streak
from agent.graph._llm_stream import _consume_llm, _consume_stream_with_stall_timeout
from agent.state import AgentState
from shared.config import settings
from shared.turn_identity import bind_turn_identity
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

    # Non-streaming fallback runs under the SAME key/value as the stream
    # segment (task #3884) and also stalls — the two adjacent stalls must
    # surface as LLMStreamStallPairError (the delayed-schedule marker), not a
    # bare TimeoutError.
    async def _ainvoke_also_stalls(*args, **kwargs):
        await asyncio.sleep(3600)  # only the fallback's own bound can cut this

    fake_llm.ainvoke = _ainvoke_also_stalls
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with pytest.raises(LLMStreamStallPairError, match="two adjacent stalls"):
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

    # Fallback also stalls → the pair error, same as the TTFT case.
    async def _ainvoke_also_stalls(*args, **kwargs):
        await asyncio.sleep(3600)

    fake_llm.ainvoke = _ainvoke_also_stalls
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with pytest.raises(LLMStreamStallPairError, match="two adjacent stalls"):
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


# ---------------------------------------------------------------------------
# Stall pair — two adjacent stalls terminate early on the SAME segment bound
# (task #3884: the 09-14/15 deepseek wave burned 600s stream + 600s fallback
# per episode, then crashed; a pair now costs ~2x the bound and schedules a
# delayed retry).
# ---------------------------------------------------------------------------


async def test_stall_pair_fallback_runs_under_the_stream_segment_bound(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-stall fallback is bounded by the SAME key/value as its stream
    segment — a hanging fallback with a tiny segment bound must be cut at that
    bound, not at the 600s fallback ceiling."""
    monkeypatch.setattr("shared.config.settings.lm.llm_stream_ttft_timeout_seconds", 0.1)
    monkeypatch.setattr(settings.lm, "llm_non_streaming_fallback_timeout_seconds", 600.0)

    async def _hang_immediately() -> AsyncIterator[AIMessageChunk]:
        await asyncio.Future()
        yield  # type: ignore[unreachable]

    async def _fallback_also_hangs(*args: object, **kwargs: object) -> AIMessage:
        await asyncio.Future()
        raise AssertionError("unreachable")

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _hang_immediately()
    fake_llm.ainvoke = _fallback_also_hangs
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    started = time.monotonic()
    with pytest.raises(LLMStreamStallPairError, match="two adjacent stalls"):
        await llm_node(state, _make_runtime(fake_llm), _CONFIG)
    elapsed = time.monotonic() - started
    # ~2 x 0.1s; the 600s fallback ceiling would make this test hang for 20min.
    assert elapsed < 5.0


class _FakeOverloadedError(Exception):
    """A provider error carrying `engine_overloaded_error` in its SDK body."""

    def __init__(self) -> None:
        super().__init__("engine overloaded")
        self.body = {"error": {"type": "engine_overloaded_error"}}


async def test_overload_fallback_timeout_is_not_a_stall_pair(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pair contract covers stalls only: an overload-triggered fallback
    that times out keeps its own (long) ceiling and propagates as the plain
    TimeoutError — never mislabelled as two adjacent stalls."""
    monkeypatch.setattr(settings.lm, "llm_non_streaming_fallback_timeout_seconds", 0.05)

    async def _stream_raises_overload() -> AsyncIterator[AIMessageChunk]:
        raise _FakeOverloadedError
        yield  # type: ignore[unreachable]

    async def _fallback_hangs(*args: object, **kwargs: object) -> AIMessage:
        await asyncio.Future()
        raise AssertionError("unreachable")

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _stream_raises_overload()
    fake_llm.ainvoke = _fallback_hangs
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with pytest.raises(TimeoutError) as exc_info:
        await llm_node(state, _make_runtime(fake_llm), _CONFIG)
    assert not isinstance(exc_info.value, LLMStreamStallPairError)


async def test_stall_events_carry_provider_health_fields(
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records,
) -> None:
    """The stall + pair events carry vendor/model/stage (the 09-14/15 wave was
    100% api.deepseek.com yet nothing in the telemetry said so) plus the
    segment's elapsed time — the fields LogQL and the OTLP histogram key on."""
    monkeypatch.setattr("shared.config.settings.lm.llm_stream_ttft_timeout_seconds", 0.05)
    monkeypatch.setattr(settings.lm, "llm_non_streaming_fallback_timeout_seconds", 0.05)

    async def _hang_immediately() -> AsyncIterator[AIMessageChunk]:
        await asyncio.Future()
        yield  # type: ignore[unreachable]

    async def _fallback_also_hangs(*args: object, **kwargs: object) -> AIMessage:
        await asyncio.Future()
        raise AssertionError("unreachable")

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _hang_immediately()
    fake_llm.ainvoke = _fallback_also_hangs
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    original = settings.lm.llm_model
    try:
        settings.lm.llm_model = "deepseek-v4-flash"
        with pytest.raises(LLMStreamStallPairError):
            await llm_node(state, _make_runtime(fake_llm), _CONFIG)
    finally:
        settings.lm.llm_model = original

    stalls = [r for r in loguru_records if r["extra"].get("event") == "stream_stalled_retry"]  # pyright: ignore[reportUnknownMemberType]
    assert len(stalls) == 1  # pyright: ignore[reportUnknownArgumentType]
    stall_extra = stalls[0]["extra"]
    assert stall_extra["vendor"] == "deepseek"
    assert stall_extra["model"] == "deepseek-v4-flash"
    assert stall_extra["stage"] == "ttft"
    assert stall_extra["elapsed_s"] >= 0.0

    pairs = [r for r in loguru_records if r["extra"].get("event") == "stream_stall_pair_terminated"]  # pyright: ignore[reportUnknownMemberType]
    assert len(pairs) == 1  # pyright: ignore[reportUnknownArgumentType]
    pair_extra = pairs[0]["extra"]
    assert pair_extra["vendor"] == "deepseek"
    assert pair_extra["stage"] == "ttft"
    assert pair_extra["timeout_s"] == pytest.approx(0.05)


async def test_entry_retry_budget_skipped_while_delayed_sequence_active(
    fake_cancel_event: asyncio.Event,
) -> None:
    """The transient wall-clock budget must not end a delayed stall sequence at
    node entry (its own streak bounds it); without an active streak the same
    elapsed time still raises LLMRetryBudgetExceededError as before."""

    def _runtime_with_elapsed(llm: MagicMock) -> Runtime[AvaContext]:
        llm.bind_tools.return_value = llm
        ctx = AvaContext(
            ops_pool=make_fake_ops_pool(),
            llm=llm,
            event_publisher=MagicMock(),
        )
        info = ExecutionInfo(
            checkpoint_id="",
            checkpoint_ns="",
            task_id="",
            node_attempt=2,
            node_first_attempt_time=time.time() - (settings.lm.llm_retry_max_total_seconds + 5.0),
        )
        return Runtime(context=ctx, execution_info=info)

    async def _normal_stream() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="ok",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _normal_stream()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with bind_turn_identity(7):
        _record_stall_pair_streak("7", 1)
        try:
            result = await llm_node(state, _runtime_with_elapsed(fake_llm), _CONFIG)
            assert result is not None
        finally:
            _reset_stall_pair_streak("7")

        # Control: no active streak -> the same elapsed time trips the budget.
        with pytest.raises(LLMRetryBudgetExceededError):
            await llm_node(state, _runtime_with_elapsed(fake_llm), _CONFIG)
