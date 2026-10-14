"""Gateway runtime and SSE lifecycle metric emission."""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import psutil
import pytest

from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from gateway.http.middleware import runtime_metrics


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
    emitted_events: EmittedEvents,
) -> None:
    loop = _Loop()
    process = _Process()
    monitor = runtime_metrics.GatewayRuntimeMonitor(
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

    assert [(event.event_name, event.attributes) for event in emitted_events] == [
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
    monitor = runtime_metrics.GatewayRuntimeMonitor(
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


def test_sse_metrics_initialize_idle_modes_at_zero(
    monkeypatch: pytest.MonkeyPatch, emitted_events: EmittedEvents
) -> None:
    monkeypatch.setattr(runtime_metrics, "sse_active_connections", {"filtered": 9})

    runtime_metrics.initialize_sse_metrics()

    assert [event.attributes for event in emitted_events] == [
        {"mode": "filtered", "active_connections": 0},
        {"mode": "throttled", "active_connections": 0},
    ]
