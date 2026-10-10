"""W14 decode_ms instrumentation — decode-stage timing on the LLM stream path.

The ops panel "\u751f\u6210 stage \u8f93\u51fa TPS" (Σout_total/Σdecode_ms) needs an honest
decode window: first chunk arrival → last chunk arrival (monotonic ms),
measured in `_consume_stream_with_stall_timeout` and stamped by
`_stream_llm` onto the handler as `llm_decode_ms`. Non-streaming
fallback calls and empty streams must carry None → NULL in the payload — a
fake window (e.g. wall-clock) would contaminate the generation-TPS panel.

The fake clock is advanced by the mock stream itself right before each yield
(never by scripted global counters): `asyncio.wait_for` reads `loop.time()`
which is `time.monotonic()` under the hood, so a blind value script would
make the assertions depend on CPython's wait_for internals. Setting the
clock inside the stream keeps the recorded timestamps exact and stable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage, HumanMessage

from agent.graph._callbacks import RedisStreamHandler
from agent.graph.llm._stream import _consume_stream_with_stall_timeout, _stream_llm
from base.agents.observation.turn_progress import TurnProgress
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import build_model_catalog


class _FakeClock:
    """Mutable monotonic stand-in; the stream advances `.t` before each yield."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _FakeHandler(RedisStreamHandler):
    """Real RedisStreamHandler subclass with a no-op publisher: the decode/latency
    stamps are the only state these tests assert on."""

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
    """The stream records (first, last) chunk-arrival timestamps; decode window
    = last - first regardless of how the loop consumed the clock."""
    clock = _FakeClock()
    monkeypatch.setattr("agent.graph.llm._stream.time.monotonic", clock)

    async def _stream() -> AsyncIterator[AIMessageChunk]:
        clock.t = 1005.0
        yield AIMessageChunk(content="a")
        clock.t = 1008.0
        yield AIMessageChunk(content="b")
        clock.t = 1013.0
        yield AIMessageChunk(content="c")

    chunks: list[AIMessageChunk] = []
    handler = _FakeHandler()
    first_ts, last_ts = await _consume_stream_with_stall_timeout(
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
    """Empty stream (StopAsyncIteration before any chunk) → (None, None): no
    honest decode window, so the payload must carry NULL."""

    async def _empty() -> AsyncIterator[AIMessageChunk]:
        if False:  # pragma: no cover — never yields
            yield AIMessageChunk(content="x")

    first_ts, last_ts = await _consume_stream_with_stall_timeout(
        _empty(),
        chunks=[],
        handler=_FakeHandler(),
        ttft_timeout=1.0,
        inter_chunk_timeout=1.0,
    )
    assert (first_ts, last_ts) == (None, None)


async def test_stream_llm_stamps_decode_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: handler.llm_decode_ms = (last - first) * 1000, stamped
    alongside llm_latency_ms after the successful attempt."""
    clock = _FakeClock()
    monkeypatch.setattr("agent.graph.llm._stream.time.monotonic", clock)

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
    await _stream_llm(
        fake_llm,
        [],
        chunks=chunks,
        handler=handler,
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        catalog=build_model_catalog(),
    )

    assert handler.llm_decode_ms == 8000.0  # (1013 - 1005) * 1000
    assert handler.llm_latency_ms == 13000.0  # (1013 - 1000) * 1000
    assert len(chunks) == 2
    assert "discarded partial" not in [chunk.content for chunk in chunks]


async def test_empty_stream_decode_ms_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No chunks at all → decode_ms stays None (latency still stamped)."""

    clock = _FakeClock()
    monkeypatch.setattr("agent.graph.llm._stream.time.monotonic", clock)

    async def _empty() -> AsyncIterator[AIMessageChunk]:
        if False:  # pragma: no cover — never yields
            yield AIMessageChunk(content="x")

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _empty()
    fake_llm.bind_tools.return_value = fake_llm

    handler = _FakeHandler()
    await _stream_llm(
        fake_llm,
        [],
        chunks=[],
        handler=handler,
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        catalog=build_model_catalog(),
    )
    assert handler.llm_decode_ms is None
    assert handler.llm_latency_ms == 0.0  # (1000 - 1000) * 1000


async def test_non_streaming_fallback_decode_ms_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stream stalls → non-streaming ainvoke fallback: the whole message
    arrives in one chunk with no first→last window → decode_ms must stay
    None (never a fake wall-clock number)."""
    monkeypatch.setattr("base.config.settings.lm.llm_stream_ttft_timeout_seconds", 0.05)

    async def _hang() -> AsyncIterator[AIMessageChunk]:
        import asyncio

        await asyncio.Future()  # never returns — simulates a dead stream
        yield  # type: ignore[unreachable]

    async def _ainvoke(messages):
        return AIMessage(content="full response from the non-streaming fallback")

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _hang()
    fake_llm.ainvoke = _ainvoke
    fake_llm.bind_tools.return_value = fake_llm

    handler = _FakeHandler()
    chunks: list[AIMessageChunk] = []
    await _stream_llm(
        fake_llm,
        [],
        chunks=chunks,
        handler=handler,
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        catalog=build_model_catalog(),
    )

    assert handler.llm_decode_ms is None
    assert handler.llm_latency_ms is not None and handler.llm_latency_ms > 0
    assert len(chunks) == 1  # single full chunk, no streaming window


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
        await _stream_llm(
            llm,
            messages,
            chunks=chunks,
            handler=handler,
            agent=AgentSlices.resolve(
                default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
            ),
            catalog=build_model_catalog(),
        )
    assert caught.value is failure
    llm.bind_tools.assert_called_once_with([execute_code])
    llm.astream.assert_called_once_with(messages)
    llm.ainvoke.assert_not_called()
    assert handler.reset_calls == 0
    assert [chunk.content for chunk in chunks] == ["partial"]
