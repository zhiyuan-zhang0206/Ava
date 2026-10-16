from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessageChunk, AnyMessage, HumanMessage

import agent.graph.llm._stream as llm_stream
from agent.graph._callbacks import RedisStreamHandler
from agent.graph.llm._stream import consume_stream_with_stall_timeout, stream_llm
from base.agents.observation.turn_progress import TurnProgress
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog


class _FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _FakeHandler(RedisStreamHandler):
    def __init__(self) -> None:
        super().__init__(
            event_publisher=MagicMock(), agent_id=1, msg_idx=0, turn_progress=TurnProgress()
        )
        self.chunks_seen: list[AIMessageChunk] = []
        self.reset_calls = 0

    def process_chunk(self, chunk: AIMessageChunk) -> None:
        self.chunks_seen.append(chunk)

    def reset(self) -> None:
        self.reset_calls += 1
        super().reset()


async def test_consume_stream_records_first_last_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(llm_stream.time, "monotonic", clock)

    async def _stream() -> AsyncIterator[AIMessageChunk]:
        clock.t = 1005.0
        yield AIMessageChunk(content="a")
        clock.t = 1008.0
        yield AIMessageChunk(content="b")
        clock.t = 1013.0
        yield AIMessageChunk(content="c")

    chunks: list[AIMessageChunk] = []
    handler = _FakeHandler()
    first_ts, last_ts = await consume_stream_with_stall_timeout(
        _stream(),
        chunks=chunks,
        handler=handler,
        ttft_timeout=1.0,
        inter_chunk_timeout=1.0,
    )
    assert (first_ts, last_ts) == (1005.0, 1013.0)
    assert len(chunks) == 3
    assert len(handler.chunks_seen) == 3


async def test_consume_stream_empty_stream_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty() -> AsyncIterator[AIMessageChunk]:
        if False:
            yield AIMessageChunk(content="x")

    first_ts, last_ts = await consume_stream_with_stall_timeout(
        _empty(),
        chunks=[],
        handler=_FakeHandler(),
        ttft_timeout=1.0,
        inter_chunk_timeout=1.0,
    )
    assert (first_ts, last_ts) == (None, None)


async def test_stream_llm_stamps_decode_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(llm_stream.time, "monotonic", clock)

    async def _stream() -> AsyncIterator[AIMessageChunk]:
        clock.t = 1005.0
        yield AIMessageChunk(content="a")
        clock.t = 1013.0
        yield AIMessageChunk(content="b")

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _stream()
    fake_llm.bind_tools.return_value = fake_llm

    handler = _FakeHandler()
    chunks: list[AIMessageChunk] = []
    await stream_llm(
        fake_llm,
        [],
        chunks=chunks,
        handler=handler,
        agent=AgentSlices.resolve(default_reader=defaults()),
        catalog=build_model_catalog(),
    )

    assert handler.llm_decode_ms == 8000.0
    assert handler.llm_latency_ms == 13000.0
    assert len(chunks) == 2
    assert "discarded partial" not in [chunk.content for chunk in chunks]


async def test_empty_stream_decode_ms_none(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(llm_stream.time, "monotonic", clock)

    async def _empty() -> AsyncIterator[AIMessageChunk]:
        if False:
            yield AIMessageChunk(content="x")

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _empty()
    fake_llm.bind_tools.return_value = fake_llm

    handler = _FakeHandler()
    await stream_llm(
        fake_llm,
        [],
        chunks=[],
        handler=handler,
        agent=AgentSlices.resolve(default_reader=defaults()),
        catalog=build_model_catalog(),
    )
    assert handler.llm_decode_ms is None
    assert handler.llm_latency_ms == 0.0


async def test_stream_preserves_prefix_and_does_not_recover_cache_403() -> None:
    from google.genai.errors import ClientError
    from langchain_core.messages import SystemMessage

    from agent.llm import execute_code

    failure = ClientError(
        403, {"error": {"message": "CachedContent not found or permission denied"}}
    )

    async def stream(_messages: list[AnyMessage]) -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content="partial")
        raise failure

    llm = MagicMock()
    llm.bind_tools.return_value = llm
    llm.astream.side_effect = stream
    messages: list[AnyMessage] = [
        SystemMessage(content="stable head"),
        HumanMessage(content="conversation"),
    ]
    chunks: list[AIMessageChunk] = []
    handler = _FakeHandler()
    with pytest.raises(ClientError) as caught:
        await stream_llm(
            llm,
            messages,
            chunks=chunks,
            handler=handler,
            agent=AgentSlices.resolve(default_reader=defaults()),
            catalog=build_model_catalog(),
        )
    assert caught.value is failure
    llm.bind_tools.assert_called_once_with([execute_code])
    llm.astream.assert_called_once_with(messages)
    llm.ainvoke.assert_not_called()
    assert handler.reset_calls == 0
    assert [chunk.content for chunk in chunks] == ["partial"]
