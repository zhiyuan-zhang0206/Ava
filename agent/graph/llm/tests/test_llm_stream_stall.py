"""LLM stream inter-chunk stall timeout — abort turn when the server hangs to prevent
indefinite blocking.

161 / 165 incident: when DeepSeek service degraded, the stream started but never
returned another chunk; the default anthropic SDK 600s overall timeout was the only
safety net, leaving the agent silently stuck for 4-10 minutes; turn_end ok=False never
even fired, so the frontend saw neither a stream nor an error.

Fix: bound the model's `__anext__` with an owned `asyncio.timeout`,
`settings.lm.llm_stream_ttft_timeout_seconds` and `settings.lm.llm_stream_inter_chunk_timeout_seconds`
raise `LLMStreamStallTimeoutError`. Together with PR #60's finally `logger.opt(exception=
True).warning`, events.payload automatically carries traceback / exception_type.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph.llm._retry import Attempt
from agent.graph.llm._stream import _consume_llm, _consume_stream_with_stall_timeout
from agent.graph.llm.node import llm_attempt
from agent.graph.llm_errors import (
    LlmLedger,
    LLMStreamStallTimeoutError,
)
from agent.state import AgentState
from agent.tests._fakes import make_fake_ops_pool
from base import config
from base.agents.context import AvaContext
from base.clock import Clock
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.native_process.turn_identity import HostedTurnResources
from base.packages.plugin_providers import build_model_catalog
from tests.fixtures.pin_agent import hosted_resources as hosted_resources

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}


async def _one_try(
    state: AgentState,
    runtime: Runtime[AvaContext],
    *,
    attempt: int = 1,
    started_ago: float = 0.0,
    ledger: LlmLedger | None = None,
) -> Any:
    """One try of the llm node (`llm_node` retries around it): a failure surfaces as raised."""
    return await llm_attempt(
        state,
        runtime,
        _CONFIG,
        Attempt(attempt, time.time() - started_ago),
        ledger or LlmLedger(),
    )


def _make_runtime(
    hosted_resources: HostedTurnResources,
    llm: MagicMock,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> Runtime[AvaContext]:
    """Same pattern as test_cancel.py: fake llm returns itself via bind_tools (chain method)."""
    llm.bind_tools.return_value = llm
    ctx = AvaContext(
        hosted_resources=hosted_resources,
        ops_pool=make_fake_ops_pool(),
        llm=llm,
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=model_catalog,
        clock_factory=Clock.from_settings,
    )
    return Runtime(context=ctx)


async def test_stall_at_ttft_raises_with_ttft_marker(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """Server completely unresponsive (zero bytes emitted) → raise LLMStreamStallTimeoutError containing 'TTFT'."""

    async def _hang_immediately() -> AsyncIterator[AIMessageChunk]:
        await asyncio.Future()  # never returns — simulates server hang
        yield  # type: ignore[unreachable]

    monkeypatch.setattr(config.settings.lm, "llm_stream_ttft_timeout_seconds", 0.05)
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _hang_immediately()

    # Non-streaming fallback runs under the SAME key/value as the stream
    # segment (task #3884) and also stalls — the two adjacent stalls must
    # surface as LLMStreamStallTimeoutError (the delayed-schedule marker), not a
    # bare TimeoutError.
    async def _ainvoke_also_stalls(*args, **kwargs):
        await asyncio.sleep(3600)  # only the fallback's own bound can cut this

    fake_llm.ainvoke = _ainvoke_also_stalls
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with pytest.raises(LLMStreamStallTimeoutError, match="stream stalled"):
        await _one_try(
            state,
            _make_runtime(
                hosted_resources=hosted_resources,
                llm=fake_llm,
                model_catalog=model_catalog,
                database_gate=database_gate,
            ),
        )


async def test_stall_mid_stream_raises_with_chunk_count(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """Emits N chunks then hangs → raise LLMStreamStallTimeoutError containing
    'mid-stream after N chunks'. Triage split: TTFT = server never connected,
    mid-stream = connected then died, ops handles them differently."""

    async def _stream_then_hang() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content="hello ")
        yield AIMessageChunk(content="world")
        yield AIMessageChunk(content="!")
        await asyncio.Future()  # hangs after 3 chunks

    monkeypatch.setattr(config.settings.lm, "llm_stream_ttft_timeout_seconds", 10.0)
    monkeypatch.setattr(config.settings.lm, "llm_stream_inter_chunk_timeout_seconds", 0.05)
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _stream_then_hang()

    # Fallback also stalls → the pair error, same as the TTFT case.
    async def _ainvoke_also_stalls(*args, **kwargs):
        await asyncio.sleep(3600)

    fake_llm.ainvoke = _ainvoke_also_stalls
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with pytest.raises(LLMStreamStallTimeoutError, match="stream stalled"):
        await _one_try(
            state,
            _make_runtime(
                hosted_resources=hosted_resources,
                llm=fake_llm,
                model_catalog=model_catalog,
                database_gate=database_gate,
            ),
        )


async def test_normal_stream_completes_no_stall_timeout(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
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
    monkeypatch.setattr(config.settings.lm, "llm_stream_ttft_timeout_seconds", 1.0)
    monkeypatch.setattr(config.settings.lm, "llm_stream_inter_chunk_timeout_seconds", 1.0)
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _normal_stream()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    # No raise — normal stream completed. The specific return value is handled by
    # llm_node's existing path (BEFORE_EXEC); this test only locks "not falsely
    # killed by stall timeout"
    result = await _one_try(
        state,
        _make_runtime(
            hosted_resources=hosted_resources,
            llm=fake_llm,
            model_catalog=model_catalog,
            database_gate=database_gate,
        ),
    )
    assert result is not None


async def test_total_timeout_ends_the_original_request_while_chunks_keep_arriving(
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
) -> None:
    """A drip-fed stream cannot evade the per-attempt total-duration ceiling."""
    import agent.graph.llm._stream as stream_module

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

    with pytest.raises(LLMStreamStallTimeoutError, match="total duration"):
        await _consume_llm(
            fake_llm,
            [],
            chunks=chunks,
            handler=MagicMock(),
            agent=AgentSlices.resolve(
                default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
            ),
            catalog=model_catalog,
        )

    assert not fallback_called
    assert streamed_chunks == 3
    assert len(chunks) == 2


async def test_stream_below_total_timeout_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.graph.llm._stream as stream_module

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
    import agent.graph.llm._stream as stream_module

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


_STREAM_BOUNDS = {
    "llm_stream_ttft_timeout_seconds": "ttft_timeout",
    "llm_stream_total_timeout_seconds": "total_timeout",
    "llm_stream_inter_chunk_timeout_seconds": "inter_chunk_timeout",
}


async def _stream_bounds(
    monkeypatch: pytest.MonkeyPatch, agent: AgentSlices
) -> dict[str, float | None]:
    """The bounds `_consume_llm` hands the stream consumer for `agent`."""
    import agent.graph.llm._stream as stream_module

    seen: dict[str, float | None] = {}

    async def _capture(*_args: object, **kwargs: Any) -> AIMessage:
        seen.update({k: kwargs[k] for k in _STREAM_BOUNDS.values()})
        return AIMessage(content="ok")

    monkeypatch.setattr(stream_module, "_consume_stream_with_stall_timeout", _capture)
    await _consume_llm(
        MagicMock(), [], chunks=[], handler=MagicMock(), agent=agent, catalog=build_model_catalog()
    )
    return seen


@pytest.mark.parametrize("setting", sorted(_STREAM_BOUNDS))
async def test_an_agents_stream_bound_is_the_one_the_stream_runs_under(
    setting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = await _stream_bounds(
        monkeypatch,
        AgentSlices.resolve(
            {setting: 4.25},
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field),
        ),
    )
    assert pinned[_STREAM_BOUNDS[setting]] == 4.25
    unpinned = await _stream_bounds(
        monkeypatch,
        AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
    )
    for other in _STREAM_BOUNDS.values():
        if other != _STREAM_BOUNDS[setting]:
            assert pinned[other] == unpinned[other]
    assert unpinned[_STREAM_BOUNDS[setting]] != 4.25
