"""W14 decode_ms instrumentation — decode-stage timing on the LLM stream path.

The ops panel "\u751f\u6210 stage \u8f93\u51fa TPS" (Σout_total/Σdecode_ms) needs an honest
decode window: first chunk arrival → last chunk arrival (monotonic ms),
measured in `_consume_stream_with_stall_timeout` and stamped by
`_stream_with_cache_retry` onto the handler as `llm_decode_ms`. Non-streaming
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
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from agent.graph._callbacks import RedisStreamHandler
from agent.graph._llm import (
    _consume_stream_with_stall_timeout,
    _stream_with_cache_retry,
)
from agent.lm_cache import LlmInvocation
from ava_builtins.plugins.lm_google import gemini_cache
from ava_builtins.plugins.lm_google.gemini_cache import CacheRef


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
        super().__init__(event_publisher=MagicMock(), agent_id=1, msg_idx=0)
        self.chunks_seen: list[AIMessageChunk] = []
        self.reset_calls = 0

    def process_chunk(self, chunk: AIMessageChunk) -> None:
        self.chunks_seen.append(chunk)

    def reset(self) -> None:
        self.reset_calls += 1
        super().reset()


def _plain_invocation(llm: MagicMock) -> LlmInvocation:
    return LlmInvocation(runnable=llm, messages=[HumanMessage(content="hi")], cache_ref=None)


def _patch_prepare(monkeypatch: pytest.MonkeyPatch, invocation_factory):
    async def _fake_prepare(llm, messages):
        return invocation_factory(llm)

    monkeypatch.setattr("agent.graph._llm_stream.prepare_invocation", _fake_prepare)  # pyright: ignore[reportUnknownArgumentType]


async def test_consume_stream_records_first_last_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream records (first, last) chunk-arrival timestamps; decode window
    = last - first regardless of how the loop consumed the clock."""
    clock = _FakeClock()
    monkeypatch.setattr("agent.graph._llm_stream.time.monotonic", clock)

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


async def test_stream_with_cache_retry_stamps_decode_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: handler.llm_decode_ms = (last - first) * 1000, stamped
    alongside llm_latency_ms after the successful attempt."""
    clock = _FakeClock()
    monkeypatch.setattr("agent.graph._llm_stream.time.monotonic", clock)

    async def _stream() -> AsyncIterator[AIMessageChunk]:
        clock.t = 1005.0
        yield AIMessageChunk(content="a")
        clock.t = 1013.0
        yield AIMessageChunk(content="b")

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _stream()
    _patch_prepare(monkeypatch, _plain_invocation)

    handler = _FakeHandler()
    chunks: list[AIMessageChunk] = []
    await _stream_with_cache_retry(fake_llm, [], chunks=chunks, handler=handler)

    assert handler.llm_decode_ms == 8000.0  # (1013 - 1005) * 1000
    assert handler.llm_latency_ms == 13000.0  # (1013 - 1000) * 1000
    assert len(chunks) == 2


async def test_empty_stream_decode_ms_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No chunks at all → decode_ms stays None (latency still stamped)."""

    clock = _FakeClock()
    monkeypatch.setattr("agent.graph._llm_stream.time.monotonic", clock)

    async def _empty() -> AsyncIterator[AIMessageChunk]:
        if False:  # pragma: no cover — never yields
            yield AIMessageChunk(content="x")

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _empty()
    _patch_prepare(monkeypatch, _plain_invocation)

    handler = _FakeHandler()
    await _stream_with_cache_retry(fake_llm, [], chunks=[], handler=handler)
    assert handler.llm_decode_ms is None
    assert handler.llm_latency_ms == 0.0  # (1000 - 1000) * 1000


async def test_non_streaming_fallback_decode_ms_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stream stalls → non-streaming ainvoke fallback: the whole message
    arrives in one chunk with no first→last window → decode_ms must stay
    None (never a fake wall-clock number)."""
    monkeypatch.setattr("shared.config.settings.lm.llm_stream_ttft_timeout_seconds", 0.05)

    async def _hang() -> AsyncIterator[AIMessageChunk]:
        import asyncio

        await asyncio.Future()  # never returns — simulates a dead stream
        yield  # type: ignore[unreachable]

    async def _ainvoke(messages):
        return AIMessage(content="full response from the non-streaming fallback")

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _hang()
    fake_llm.ainvoke = _ainvoke
    _patch_prepare(monkeypatch, _plain_invocation)

    handler = _FakeHandler()
    chunks: list[AIMessageChunk] = []
    await _stream_with_cache_retry(fake_llm, [], chunks=chunks, handler=handler)

    assert handler.llm_decode_ms is None
    assert handler.llm_latency_ms is not None and handler.llm_latency_ms > 0
    assert len(chunks) == 1  # single full chunk, no streaming window


async def test_stale_cache_retry_uses_second_attempt_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale-cache retry: the FIRST attempt's partial/failed window is
    discarded — decode_ms comes from the successful second attempt only."""
    clock = _FakeClock()
    monkeypatch.setattr("agent.graph._llm_stream.time.monotonic", clock)

    class _StaleCacheError(Exception):
        pass

    attempts = {"n": 0}

    async def _flaky_stream() -> AsyncIterator[AIMessageChunk]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _StaleCacheError("CachedContent not found")
        clock.t = 1005.0
        yield AIMessageChunk(content="a")
        clock.t = 1008.0
        yield AIMessageChunk(content="b")

    fake_llm = MagicMock()
    # Fresh generator per attempt: a generator that raised is closed — the
    # retry would otherwise see an empty stream on the second astream() call.
    fake_llm.astream.side_effect = lambda _messages: _flaky_stream()
    invocations = {"n": 0}

    def _invocation_factory(llm):
        invocations["n"] += 1
        if invocations["n"] == 1:
            return LlmInvocation(
                runnable=llm,  # pyright: ignore[reportUnknownArgumentType]
                messages=[HumanMessage(content="hi")],
                cache_ref=CacheRef(
                    name="cache-1",
                    key="k",
                    expire_time=datetime.now(UTC) + timedelta(hours=1),
                ),
            )
        return _plain_invocation(llm)  # pyright: ignore[reportUnknownArgumentType]

    _patch_prepare(monkeypatch, _invocation_factory)
    monkeypatch.setattr(gemini_cache, "is_stale_cache_error", lambda _exc: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(gemini_cache, "invalidate", lambda _ref: None)  # pyright: ignore[reportUnknownArgumentType]

    handler = _FakeHandler()
    chunks: list[AIMessageChunk] = []
    await _stream_with_cache_retry(fake_llm, [], chunks=chunks, handler=handler)

    assert handler.reset_calls == 1
    assert handler.llm_decode_ms == 3000.0  # (1008 - 1005) * 1000 — 2nd attempt only
    assert handler.llm_latency_ms == 8000.0  # (1008 - 1000) * 1000 — whole call
    assert len(chunks) == 2
