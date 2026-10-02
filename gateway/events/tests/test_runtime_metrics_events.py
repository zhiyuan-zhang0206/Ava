"""The SSE streams open and close the active-connection gauge in both modes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

import pytest
from fastapi import Request

from gateway.events import sse
from gateway.middleware import runtime_metrics


class _Request:
    async def is_disconnected(self) -> bool:
        return True


class _PubSub:
    async def subscribe(self, _channel: str) -> None:
        return None

    async def unsubscribe(self, _channel: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _RedisClient:
    def __init__(self) -> None:
        self._pubsub = _PubSub()

    def pubsub(self) -> _PubSub:
        return self._pubsub

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize(
    "mode",
    ("filtered", "throttled"),
)
def test_sse_stream_lifecycle_increments_and_decrements_active_gauge(
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["filtered", "throttled"],
) -> None:
    emitted: list[dict[str, Any]] = []

    def open_redis(_url: str) -> _RedisClient:
        return _RedisClient()

    monkeypatch.setattr(sse, "open_async_redis", open_redis)

    async def pass_through(call: Callable[[], Awaitable[Any]]) -> Any:
        return await call()

    monkeypatch.setattr(sse, "retry_auth_failures_async", pass_through)

    def capture_emit(_category: str, _event_name: str, *, attributes: dict[str, Any]) -> None:
        emitted.append(attributes)

    monkeypatch.setattr(runtime_metrics.telemetry, "emit", capture_emit)
    runtime_metrics.initialize_sse_metrics()
    emitted.clear()

    async def run_stream() -> None:
        request = cast(Request, _Request())
        if mode == "filtered":
            stream = sse.event_stream("redis://test", 7, request)
        else:
            stream = sse.throttled_event_stream("redis://test", request, throttle_rate=10.0)
        assert await anext(stream) == b": stream open\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(run_stream())

    assert emitted == [
        {"mode": mode, "active_connections": 1, "opened": 1},
        {"mode": mode, "active_connections": 0, "closed": 1},
    ]
