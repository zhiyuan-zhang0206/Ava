"""Gateway process, event-loop, and SSE connection metrics.

The runtime monitor schedules a one-second callback on uvicorn's event loop.
Every minute it emits the worst callback scheduling delay, the number of slow
ticks, and one non-blocking psutil snapshot. A blocked loop therefore records
the stall on the first callback that can run after it recovers.

SSE lifecycle calls run on that same loop. Their process-local counts need no
lock and reset naturally when the gateway restarts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Literal, Protocol

import psutil

from shared import telemetry

_log = logging.getLogger(__name__)

TICK_INTERVAL_S = 1.0
EMIT_INTERVAL_S = 60.0
SLOW_TICK_THRESHOLD_MS = 100.0

SseMode = Literal["filtered", "throttled"]
_SSE_MODES: tuple[SseMode, ...] = ("filtered", "throttled")
_sse_active_connections: dict[SseMode, int] = dict.fromkeys(_SSE_MODES, 0)


class _Cancelable(Protocol):
    def cancel(self) -> None: ...


class _LoopClock(Protocol):
    def time(self) -> float: ...

    def call_at(
        self,
        when: float,
        callback: Callable[..., object],
        *args: object,
    ) -> _Cancelable: ...


def sse_opened(mode: SseMode) -> None:
    """Record one established SSE stream and its new active depth."""
    active = _sse_active_connections.get(mode, 0) + 1
    _sse_active_connections[mode] = active
    telemetry.emit(
        "telemetry",
        "sse",
        attributes={"mode": mode, "active_connections": active, "opened": 1},
    )


def sse_closed(mode: SseMode) -> None:
    """Record one closed SSE stream and its remaining active depth."""
    active = _sse_active_connections[mode] - 1
    if active < 0:
        raise RuntimeError(f"SSE active connection count became negative for mode {mode!r}")
    _sse_active_connections[mode] = active
    telemetry.emit(
        "telemetry",
        "sse",
        attributes={"mode": mode, "active_connections": active, "closed": 1},
    )


def _initialize_sse_metrics() -> None:
    """Publish zero connection depth so an idle gateway still has series."""
    _sse_active_connections.clear()
    _sse_active_connections.update(dict.fromkeys(_SSE_MODES, 0))
    for mode in _SSE_MODES:
        telemetry.emit(
            "telemetry",
            "sse",
            attributes={"mode": mode, "active_connections": 0},
        )


class GatewayRuntimeMonitor:
    """Periodic gateway process sampler and event-loop stall detector."""

    def __init__(
        self,
        *,
        loop: _LoopClock,
        process: psutil.Process,
        tick_interval_s: float = TICK_INTERVAL_S,
        emit_interval_s: float = EMIT_INTERVAL_S,
        slow_tick_threshold_ms: float = SLOW_TICK_THRESHOLD_MS,
    ) -> None:
        self._loop = loop
        self._process = process
        self._tick_interval_s = tick_interval_s
        self._emit_interval_s = emit_interval_s
        self._slow_tick_threshold_ms = slow_tick_threshold_ms
        self._last_emit_at = 0.0
        self._max_lag_ms = 0.0
        self._slow_ticks = 0
        self._handle: _Cancelable | None = None
        self._stopped = False

    def start(self) -> None:
        """Prime process CPU accounting and schedule the first loop tick."""
        if self._handle is not None:
            raise RuntimeError("gateway runtime monitor already started")
        self._last_emit_at = self._loop.time()
        try:
            self._process.cpu_percent(interval=None)
        except Exception:
            _log.warning("gateway runtime metric CPU baseline failed", exc_info=True)
        self._schedule_next(self._last_emit_at)

    def stop(self) -> None:
        """Cancel the scheduled callback during gateway lifespan teardown."""
        self._stopped = True
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _schedule_next(self, now: float) -> None:
        expected_at = now + self._tick_interval_s
        self._handle = self._loop.call_at(expected_at, self._tick, expected_at)

    def _tick(self, expected_at: float) -> None:
        """Measure callback delay, flush when due, and keep the monitor alive."""
        if self._stopped:
            return
        now = self._loop.time()
        lag_ms = max(0.0, (now - expected_at) * 1000.0)
        self._max_lag_ms = max(self._max_lag_ms, lag_ms)
        if lag_ms >= self._slow_tick_threshold_ms:
            self._slow_ticks += 1
        if now - self._last_emit_at >= self._emit_interval_s:
            try:
                self._emit_snapshot()
            except Exception:
                _log.warning("gateway runtime metric sample failed", exc_info=True)
            self._last_emit_at = now
            self._max_lag_ms = 0.0
            self._slow_ticks = 0
        self._schedule_next(now)

    def _emit_snapshot(self) -> None:
        telemetry.emit(
            "telemetry",
            "gateway_event_loop",
            attributes={
                "lag_ms": round(self._max_lag_ms, 1),
                "slow_ticks": self._slow_ticks,
            },
        )
        memory = self._process.memory_info()
        telemetry.emit(
            "telemetry",
            "gateway_process",
            attributes={
                "cpu_percent": self._process.cpu_percent(interval=None),
                "rss_bytes": memory.rss,
                "fd_count": self._process.num_fds(),
            },
        )


def start_runtime_monitor() -> GatewayRuntimeMonitor:
    """Start one monitor bound to the running uvicorn event loop."""
    _initialize_sse_metrics()
    monitor = GatewayRuntimeMonitor(loop=asyncio.get_running_loop(), process=psutil.Process())
    monitor.start()
    return monitor
