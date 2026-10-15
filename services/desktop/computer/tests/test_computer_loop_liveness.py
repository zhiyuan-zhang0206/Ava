from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

import services.desktop.computer.mcp_daemon as daemon_mod
from base.db import Database
from services.desktop.computer.tests.slices import short_sock_dir
from services.desktop.computer.tests.test_computer_mcp_daemon import FakeHelper


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_loop_watchdog_fires_when_the_loop_stops_beating() -> None:
    clock = _FakeClock()
    fired = threading.Event()
    watchdog = daemon_mod.LoopWatchdog(0.05, check_s=0.005, on_stall=fired.set, clock=clock)
    watchdog.start()
    try:
        clock.t = 1.0
        assert fired.wait(2.0), "watchdog never fired for a silent loop"
    finally:
        watchdog.stop()
    assert watchdog.stopped


def test_loop_watchdog_is_silent_while_beaten() -> None:
    clock = _FakeClock()
    fired = threading.Event()
    watchdog = daemon_mod.LoopWatchdog(0.05, check_s=0.005, on_stall=fired.set, clock=clock)
    watchdog.start()
    try:
        for _ in range(20):
            clock.t += 0.01
            watchdog.beat()
            time.sleep(0.005)
        assert not fired.is_set()
    finally:
        watchdog.stop()
        watchdog.join(2.0)
    assert not watchdog.is_alive()


async def test_guard_loop_liveness_beats_from_the_loop() -> None:
    fired = threading.Event()
    watchdog = daemon_mod.guard_loop_liveness(
        asyncio.get_running_loop(), 0.3, beat_s=0.01, check_s=5.0, on_stall=fired.set
    )
    try:
        await asyncio.sleep(0.5)
        assert watchdog.stale_for() < 0.3
        assert not fired.is_set()
    finally:
        watchdog.stop()
        watchdog.join(timeout=1.0)
    assert not watchdog.is_alive()


async def test_run_arms_loop_watchdog(monkeypatch: pytest.MonkeyPatch, database: Database) -> None:
    d, sock, cleanup = short_sock_dir()
    events: list[str] = []

    class FakeWatchdog:
        def __init__(self, stall_s: float, *, check_s: float, on_stall: Any) -> None:
            events.append(f"init:{stall_s}")

        def start(self) -> None:
            events.append("start")

        def beat(self) -> None:
            events.append("beat")

        def stop(self) -> None:
            events.append("stop")

        @property
        def stopped(self) -> bool:
            return False

    class StopEvent:
        def set(self) -> None: ...

        async def wait(self) -> None:
            return None

    class FakeServer:
        def close(self) -> None: ...

        async def wait_closed(self) -> None: ...

    async def _not_in_use(_path: Path) -> bool:
        return False

    async def _fake_server(*_args: Any, **_kwargs: Any) -> FakeServer:
        return FakeServer()

    monkeypatch.setattr(daemon_mod, "socket_in_use", _not_in_use)
    monkeypatch.setattr(daemon_mod.asyncio, "start_unix_server", _fake_server)
    monkeypatch.setattr(daemon_mod, "LoopWatchdog", FakeWatchdog)
    monkeypatch.setattr(daemon_mod.asyncio, "Event", StopEvent)

    try:
        await daemon_mod.run(sock=str(sock), database=lambda: database, helper=FakeHelper())
    finally:
        cleanup(d)

    assert events[0] == f"init:{daemon_mod.settings.daemon.computer_use_loop_stall_s}"
    assert events.count("start") == 1
    assert "beat" in events
    assert events[-1] == "stop"


class _FakeServer:
    def __init__(self) -> None:
        self.waited = False

    async def wait_closed(self) -> None:
        self.waited = True


async def test_bounded_cleanup_cancels_clients_and_waits_for_the_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hung = asyncio.Event()

    async def client() -> None:
        await hung.wait()

    task = asyncio.create_task(client())
    await asyncio.sleep(0)
    server = _FakeServer()
    dumped: list[str] = []
    monkeypatch.setattr(daemon_mod, "dump_and_exit", dumped.append)

    await daemon_mod.bounded_cleanup(cast(asyncio.AbstractServer, server), {task}, drain_s=1.0)

    assert task.cancelled()
    assert server.waited
    assert dumped == []


class _DumpAndExitCalledError(Exception):
    pass


async def test_bounded_cleanup_dumps_and_exits_when_a_client_survives_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def stubborn() -> None:
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if release.is_set():
                    raise

    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    server = _FakeServer()
    dumped: list[str] = []

    def _record_and_exit(reason: str) -> None:
        dumped.append(reason)
        raise _DumpAndExitCalledError(reason)

    monkeypatch.setattr(daemon_mod, "dump_and_exit", _record_and_exit)

    try:
        with pytest.raises(_DumpAndExitCalledError, match=re.escape("did not finish within 0.05s")):
            await daemon_mod.bounded_cleanup(
                cast(asyncio.AbstractServer, server), {task}, drain_s=0.05
            )

        assert dumped == ["shutdown cleanup did not finish within 0.05s"]
        assert not server.waited
    finally:
        release.set()
        task.cancel()
        _, still_running = await asyncio.wait({task}, timeout=2.0)
        assert not still_running, "stubborn handler survived the test cleanup"
