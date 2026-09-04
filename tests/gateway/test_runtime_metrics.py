"""Gateway runtime and SSE lifecycle metric emission."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, Literal, cast

import psutil
import pytest
from fastapi import Request

from gateway import _runtime_metrics, sse


class _TimerHandle:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _Loop:
    def __init__(self) -> None:
        self.now = 0.0
        self.scheduled: list[
            tuple[float, Callable[..., None], tuple[object, ...], _TimerHandle]
        ] = []

    def time(self) -> float:
        return self.now

    def call_at(
        self,
        when: float,
        callback: Callable[..., None],
        *args: object,
    ) -> _TimerHandle:
        handle = _TimerHandle()
        self.scheduled.append((when, callback, args, handle))
        return handle


class _Process:
    def __init__(self) -> None:
        self.cpu_calls = 0

    def cpu_percent(self, interval: float | None = None) -> float:
        assert interval is None
        self.cpu_calls += 1
        return 0.0 if self.cpu_calls == 1 else 37.5

    def memory_info(self) -> SimpleNamespace:
        return SimpleNamespace(rss=268_435_456)

    def num_fds(self) -> int:
        return 23


def test_runtime_monitor_callback_emits_process_and_loop_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []

    def capture_emit(_category: str, event_name: str, *, attributes: dict[str, Any]) -> None:
        emitted.append((event_name, attributes))

    monkeypatch.setattr(_runtime_metrics.telemetry, "emit", capture_emit)
    loop = _Loop()
    process = _Process()
    monitor = _runtime_metrics.GatewayRuntimeMonitor(
        loop=loop,  # type: ignore[arg-type]
        process=cast(psutil.Process, process),
        tick_interval_s=1.0,
        emit_interval_s=1.0,
        slow_tick_threshold_ms=100.0,
    )

    monitor.start()
    scheduled_at, callback, args, first_handle = loop.scheduled.pop()
    assert scheduled_at == 1.0
    loop.now = 1.25
    callback(*args)

    assert emitted == [
        (
            "gateway_event_loop",
            {
                "lag_ms": 250.0,
                "slow_ticks": 1,
            },
        ),
        (
            "gateway_process",
            {"cpu_percent": 37.5, "rss_bytes": 268_435_456, "fd_count": 23},
        ),
    ]
    assert process.cpu_calls == 2
    assert loop.scheduled[-1][0] == 2.25

    monitor.stop()
    assert loop.scheduled[-1][3].cancelled
    assert not first_handle.cancelled


def test_runtime_monitor_reschedules_after_sampling_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop = _Loop()
    process = _Process()
    monitor = _runtime_metrics.GatewayRuntimeMonitor(
        loop=loop,  # type: ignore[arg-type]
        process=cast(psutil.Process, process),
        tick_interval_s=1.0,
        emit_interval_s=1.0,
        slow_tick_threshold_ms=100.0,
    )
    monkeypatch.setattr(process, "memory_info", lambda: (_ for _ in ()).throw(OSError("gone")))

    monitor.start()
    _scheduled_at, callback, args, _handle = loop.scheduled.pop()
    loop.now = 1.0
    with caplog.at_level(logging.WARNING):
        callback(*args)

    assert "gateway runtime metric sample failed" in caplog.text
    assert loop.scheduled[-1][0] == 2.0


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


def test_sse_metrics_initialize_idle_modes_at_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(_runtime_metrics, "_sse_active_connections", {"filtered": 9})

    def capture_emit(_category: str, _event_name: str, *, attributes: dict[str, Any]) -> None:
        emitted.append(attributes)

    monkeypatch.setattr(_runtime_metrics.telemetry, "emit", capture_emit)

    _runtime_metrics._initialize_sse_metrics()

    assert emitted == [
        {"mode": "filtered", "active_connections": 0},
        {"mode": "throttled", "active_connections": 0},
    ]


@pytest.mark.parametrize(
    "mode",
    ("filtered", "throttled"),
)
def test_sse_stream_lifecycle_increments_and_decrements_active_gauge(
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["filtered", "throttled"],
) -> None:
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(_runtime_metrics, "_sse_active_connections", {})

    def open_redis(_url: str) -> _RedisClient:
        return _RedisClient()

    monkeypatch.setattr(sse, "open_async_redis", open_redis)

    async def pass_through(call: Callable[[], Awaitable[Any]]) -> Any:
        return await call()

    monkeypatch.setattr(sse, "retry_auth_failures_async", pass_through)

    def capture_emit(_category: str, _event_name: str, *, attributes: dict[str, Any]) -> None:
        emitted.append(attributes)

    monkeypatch.setattr(_runtime_metrics.telemetry, "emit", capture_emit)

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
