"""Retry authority belongs to typed provider transport and owned timer expiry."""

import asyncio
from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from langchain_core.exceptions import ModelConnectionError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk

from agent.graph._callbacks import RedisStreamHandler
from agent.graph.llm._retry import retry_wait
from agent.graph.llm._stream import _consume_llm, _consume_stream_with_stall_timeout
from agent.graph.llm_errors import LlmLedger
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.lm.errors import is_retryable_provider_error
from base.lm.plugin_providers import build_model_catalog


@pytest.mark.parametrize("during_model_iteration", [False, True])
async def test_transport_authority_stays_at_the_model_boundary(
    during_model_iteration: bool,
) -> None:
    error = httpx.ConnectError("connection interrupted")

    async def stream() -> AsyncIterator[AIMessage]:
        if during_model_iteration:
            raise error
        yield AIMessageChunk(content="one chunk")

    sink = MagicMock(spec=RedisStreamHandler)
    if not during_model_iteration:
        sink.process_chunk.side_effect = error
    handler = cast(RedisStreamHandler, sink)
    expected = ModelConnectionError if during_model_iteration else httpx.ConnectError
    with pytest.raises(expected) as raised:
        await _consume_stream_with_stall_timeout(
            stream(), chunks=[], handler=handler, ttft_timeout=1, inter_chunk_timeout=1
        )
    if during_model_iteration:
        assert raised.value.__cause__ is error
        assert is_retryable_provider_error(raised.value)
    else:
        assert raised.value is error
        assert not is_retryable_provider_error(error)


@pytest.mark.parametrize(
    "timeout_origin", ["stream_model", "stream_callback", "fallback_model", "fallback_callback"]
)
async def test_builtin_timeout_does_not_borrow_owned_stall_authority(
    monkeypatch: pytest.MonkeyPatch, timeout_origin: str
) -> None:
    error = TimeoutError("application operation failed")
    calls: list[str] = []

    async def stream() -> AsyncIterator[AIMessage]:
        calls.append("stream")
        if timeout_origin == "stream_model":
            raise error
        if timeout_origin.startswith("fallback"):
            await asyncio.Future()
        yield AIMessageChunk(content="streamed")

    async def invoke(messages: list[object]) -> AIMessage:
        calls.append("fallback")
        if timeout_origin == "fallback_model":
            raise error
        return AIMessage(content="successful fallback")

    monkeypatch.setattr(settings.lm, "llm_stream_ttft_timeout_seconds", 0.01)
    monkeypatch.setattr(settings.lm, "llm_stream_inter_chunk_timeout_seconds", 0.01)
    sink = MagicMock(spec=RedisStreamHandler)
    if timeout_origin.endswith("callback"):
        sink.process_chunk.side_effect = error
    model = MagicMock(spec=BaseChatModel)
    model.astream.return_value = stream()
    model.ainvoke = AsyncMock(side_effect=invoke)
    agent = AgentSlices.resolve()
    with pytest.raises(TimeoutError) as raised:
        await _consume_llm(
            model,
            [],
            chunks=[],
            handler=cast(RedisStreamHandler, sink),
            agent=agent,
            catalog=build_model_catalog(),
        )
    assert raised.value is error
    assert (
        retry_wait(
            error,
            1,
            model=agent.brain.llm_model,
            agent_id=7,
            ledger=LlmLedger(),
            catalog=build_model_catalog(),
            max_attempts_pin=AgentSlices.resolve().read("lm", "llm_retry_max_attempts"),
        )
        is None
    )
    expected = ["stream", "fallback"] if timeout_origin.startswith("fallback") else ["stream"]
    assert calls == expected


@pytest.mark.parametrize("during_fallback", [False, True])
async def test_external_cancellation_stays_external_inside_owned_model_deadline(
    monkeypatch: pytest.MonkeyPatch, during_fallback: bool
) -> None:
    ready = asyncio.Event()
    cancelled: list[str] = []

    async def stream() -> AsyncIterator[AIMessage]:
        if not during_fallback:
            ready.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.append("stream")
            raise
        yield AIMessageChunk(content="unreachable")

    async def invoke(messages: list[object]) -> AIMessage:
        ready.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.append("fallback")
            raise
        return AIMessage(content="unreachable")

    monkeypatch.setattr(
        settings.lm, "llm_stream_ttft_timeout_seconds", 0.01 if during_fallback else 10
    )
    monkeypatch.setattr(settings.lm, "llm_stream_inter_chunk_timeout_seconds", 10)
    sink = MagicMock(spec=RedisStreamHandler)
    model = MagicMock(spec=BaseChatModel)
    model.astream.return_value = stream()
    model.ainvoke = AsyncMock(side_effect=invoke)
    task = asyncio.create_task(
        _consume_llm(
            model,
            [],
            chunks=[],
            handler=cast(RedisStreamHandler, sink),
            agent=AgentSlices.resolve(),
            catalog=build_model_catalog(),
        )
    )
    await asyncio.wait_for(ready.wait(), 1)
    task.cancel("user cancelled")
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert raised.value.args == ("user cancelled",)
    assert cancelled == (["stream", "fallback"] if during_fallback else ["stream"])
    sink.process_chunk.assert_not_called()
