"""Loop-liveness and shutdown-drain bounds of the computer-use MCP daemon.

The 2026-10-04 wedge (postmortem 0011) had two halves, guarded here: a
blocked event loop that never reached its signal handler (the watchdog dumps
stacks and exits for a supervisor restart), and a shutdown whose drain waited
without a bound on work it had just cancelled (bounded the same way). Both
guards turn "half-alive and unstoppable" into "a dead process the supervisor
restarts".
"""

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

# ── loop-liveness watchdog (2026-10-04 wedged-loop regression) ─────────────


class _FakeClock:
    """A hand-driven monotonic clock — watchdog tests never race wall time."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_loop_watchdog_fires_when_the_loop_stops_beating() -> None:
    """A silent run loop must reach on_stall (production: dump + exit) — the
    guardrail the 2026-10-04 wedge lacked: the loop stopped ticking and the
    unit sat unstoppable until a manual kill."""
    clock = _FakeClock()
    fired = threading.Event()
    watchdog = daemon_mod._LoopWatchdog(0.05, check_s=0.005, on_stall=fired.set, clock=clock)
    watchdog.start()
    try:
        clock.t = 1.0  # the loop stopped beating; the clock marches on
        assert fired.wait(2.0), "watchdog never fired for a silent loop"
    finally:
        watchdog.stop()
    assert watchdog.stopped


def test_loop_watchdog_is_silent_while_beaten() -> None:
    """Beats inside the window keep the watchdog quiet indefinitely."""
    clock = _FakeClock()
    fired = threading.Event()
    watchdog = daemon_mod._LoopWatchdog(0.05, check_s=0.005, on_stall=fired.set, clock=clock)
    watchdog.start()
    try:
        for _ in range(20):
            clock.t += 0.01  # a beat every 10ms of (fake) time, inside the window
            watchdog.beat()
            time.sleep(0.005)  # let the checker observe the fresh beat
        assert not fired.is_set()
    finally:
        watchdog.stop()
        watchdog.join(2.0)
    assert not watchdog.is_alive()


async def test_guard_loop_liveness_beats_from_the_loop() -> None:
    """The beat source is the loop's own tick callback: a live loop keeps
    re-stamping the watchdog (no external heartbeat can mask a blocked loop)."""
    fired = threading.Event()
    # check_s set past the test's horizon: the checker cannot fire here, so a
    # fresh stale_for value proves the loop's tick itself did the beating.
    watchdog = daemon_mod._guard_loop_liveness(
        asyncio.get_running_loop(), 0.3, beat_s=0.01, check_s=5.0, on_stall=fired.set
    )
    try:
        await asyncio.sleep(0.5)  # ~50 beat ticks; without them stale_for >= 0.5
        assert watchdog.stale_for() < 0.3
        assert not fired.is_set()
    finally:
        watchdog.stop()
        watchdog.join(timeout=1.0)
    assert not watchdog.is_alive()


async def test_run_arms_loop_watchdog(monkeypatch: pytest.MonkeyPatch, database: Database) -> None:
    """run() starts the watchdog with the configured window and stops it on
    the shutdown path — without this wiring the stall guardrail is inert."""
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

    monkeypatch.setattr(daemon_mod, "_socket_in_use", _not_in_use)
    monkeypatch.setattr(daemon_mod.asyncio, "start_unix_server", _fake_server)
    monkeypatch.setattr(daemon_mod, "_LoopWatchdog", FakeWatchdog)
    monkeypatch.setattr(daemon_mod.asyncio, "Event", StopEvent)

    try:
        await daemon_mod.run(sock=str(sock), database=lambda: database)
    finally:
        cleanup(d)

    assert events[0] == f"init:{daemon_mod.settings.daemon.computer_use_loop_stall_s}"
    assert events.count("start") == 1
    assert "beat" in events
    assert events[-1] == "stop"


# ── bounded shutdown drain (2026-10-04 half-down regression) ───────────────


class _FakeServer:
    """AbstractServer stand-in: records that the listener wait ran."""

    def __init__(self) -> None:
        self.waited = False

    async def wait_closed(self) -> None:
        self.waited = True


async def test_bounded_cleanup_cancels_clients_and_waits_for_the_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graceful path: every tracked client is cancelled, the listener
    wait runs, and the drain finishes inside the bound — no dump, no exit."""
    hung = asyncio.Event()

    async def client() -> None:
        await hung.wait()  # runs until cancelled — the persistent SDK socket

    task = asyncio.create_task(client())
    await asyncio.sleep(0)
    server = _FakeServer()
    dumped: list[str] = []
    monkeypatch.setattr(daemon_mod, "_dump_and_exit", dumped.append)

    await daemon_mod._bounded_cleanup(cast(asyncio.AbstractServer, server), {task}, drain_s=1.0)

    assert task.cancelled()
    assert server.waited
    assert dumped == []


class _DumpAndExitCalledError(Exception):
    """Models `_dump_and_exit`'s contract: in production it never returns
    (`os._exit`), so a test stand-in must not let execution continue past it."""


async def test_bounded_cleanup_dumps_and_exits_when_a_client_survives_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler that swallows cancellation must not hold shutdown open: past
    the drain bound the daemon dumps stacks and exits for a supervisor
    restart (the 2026-10-04 half-down: listener closed, stop signal consumed,
    process neither serving nor dying). This is also why the drain waits with
    `asyncio.wait`, not `gather` — a `gather`'s await cannot be un-parked by
    any timeout while such a handler lives (its `cancel()` only forwards into
    the children), so a `gather`-based bound would hang together with its
    drain."""
    release = asyncio.Event()

    async def stubborn() -> None:
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if release.is_set():
                    raise
                # swallowed on purpose: the drain must not wait for this handler

    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    server = _FakeServer()
    dumped: list[str] = []

    def _record_and_exit(reason: str) -> None:
        dumped.append(reason)
        raise _DumpAndExitCalledError(reason)

    monkeypatch.setattr(daemon_mod, "_dump_and_exit", _record_and_exit)

    try:
        with pytest.raises(_DumpAndExitCalledError, match=re.escape("did not finish within 0.05s")):
            await daemon_mod._bounded_cleanup(
                cast(asyncio.AbstractServer, server), {task}, drain_s=0.05
            )

        assert dumped == ["shutdown cleanup did not finish within 0.05s"]
        assert not server.waited  # the listener wait never completed
    finally:
        # Unconditional: a broken bound must leave a *failed test*, not a live
        # handler that traps the event loop's own teardown on the cancellation
        # it swallows (the same gather-pinning this drain exists to escape).
        release.set()
        task.cancel()
        _, still_running = await asyncio.wait({task}, timeout=2.0)
        assert not still_running, "stubborn handler survived the test cleanup"
