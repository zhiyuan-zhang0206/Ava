"""Schedule stall guards distinguish native parking from unfinished work."""

from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import contextmanager, suppress

import pytest

from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate

# ── stall guard (2026-08-03: a hung schedule sat "alive" through the gateway
#    freeze window and silently missed its Monday fire — the guard records the
#    stall and hard-exits so the schedule is not a silent zombie) ────────────


@contextmanager
def _watch_stalls(
    monkeypatch: pytest.MonkeyPatch, *, patch_sleep: bool = False, database_gate: ProcessDbGate
) -> Generator[list[str], None, None]:
    """Record real guard verdicts without exiting the test process."""
    import gateway.schedules.runner as sr

    monkeypatch.setattr(settings.gateway, "schedule_stall_timeout_seconds", 0.1)
    fired: list[str] = []

    def record_stall(_db: object, _sid: int, msg: str, _rid: int | None) -> None:
        fired.append(msg)

    monkeypatch.setattr(sr, "_stall_action", record_stall)
    if patch_sleep:
        sr._patch_park_detection()
    stop = sr._StallGuard(Database.from_settings(gate=database_gate), 1, None)
    try:
        yield fired
    finally:
        stop.close()
        if patch_sleep:
            sr._restore_park_detection()


def test_stall_guard_fires_on_a_stalled_main_thread(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """A main-thread frame that does not advance past the timeout trips the
    guard (the stall verdict is recorded; the hard exit itself is os._exit in
    the guard thread and is not exercised in-process)."""
    import time

    with _watch_stalls(monkeypatch, database_gate=database_gate) as fired:
        deadline = time.monotonic() + 2.0
        while not fired and time.monotonic() < deadline:
            x = 0
            x += 1  # a stable, non-park frame for the guard to observe
        assert fired, "stall guard never fired on a stalled main thread"
        assert "stalled" in fired[0]


def test_stall_guard_ignores_a_legitimately_sleeping_main_thread(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """A resident schedule parks in time.sleep between fire windows — that is
    the design, not a stall, so the guard must stay quiet."""
    import time

    import gateway.schedules.runner as sr

    monkeypatch.setattr(settings.gateway, "schedule_stall_timeout_seconds", 0.05)
    fired: list[str] = []
    monkeypatch.setattr(
        sr,
        "_stall_action",
        lambda _db, _sid, msg, _rid: fired.append(msg),  # pyright: ignore[reportUnknownArgumentType]
    )

    # The runner wraps time.sleep before running a script; without the wrapper
    # the deepest Python frame of a sleeping main thread is sleep's *caller*,
    # which is indistinguishable from a stall.
    _real_sleep = time.sleep
    sr._patch_park_detection()
    stop = sr._StallGuard(Database.from_settings(gate=database_gate), 1, None)
    try:
        time.sleep(0.4)  # main thread parked in time.sleep the whole time
        assert fired == []
    finally:
        stop.close()
        # The wrapper is process-wide (`time.sleep = sleep` in
        # schedule_runner._patch_park_detection) — restore it or every later
        # `assert time.sleep is _REAL_SLEEP` guard (test_start_readiness_gate)
        # in this worker session fails on the wrapper.
        time.sleep = _real_sleep


@pytest.mark.parametrize("capture_output", [True, False], ids=["captured", "uncaptured"])
def test_stall_guard_ignores_a_live_child_wait(
    monkeypatch: pytest.MonkeyPatch, capture_output: bool, database_gate: ProcessDbGate
) -> None:
    """A child wait may outlast the stall budget; its caller owns the timeout."""
    import subprocess

    with _watch_stalls(monkeypatch, database_gate=database_gate) as fired:
        subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(0.5)"],
            capture_output=capture_output,
            timeout=2,
            check=True,
        )
        assert fired == []


def test_stall_guard_fires_on_select_outside_subprocess(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """The select frame used by child waits must still expose socket wedges."""
    import selectors
    import socket

    reader, writer = socket.socketpair()
    with reader, writer, selectors.DefaultSelector() as selector:
        selector.register(reader, selectors.EVENT_READ)
        with _watch_stalls(monkeypatch, database_gate=database_gate) as fired:
            selector.select(timeout=0.5)
            assert fired, "stall guard ignored a socket wait outside subprocess"
            assert "select" in fired[0]


def test_stall_guard_fires_during_subprocess_spawn(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """The caller's timeout does not bound Popen's wait for child startup."""
    import subprocess
    import time

    def slow_spawn() -> None:
        time.sleep(0.5)

    with _watch_stalls(monkeypatch, patch_sleep=True, database_gate=database_gate) as fired:
        subprocess.run(
            [sys.executable, "-c", "pass"],
            capture_output=True,
            timeout=0.2,
            preexec_fn=slow_spawn,
            check=True,
        )
        assert fired, "stall guard ignored subprocess spawn"
        assert "_execute_child" in fired[0]


def test_stall_guard_fires_during_subprocess_argument_conversion(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """A blocking PathLike callback is not part of the bounded child wait."""
    import os
    import socket
    import subprocess
    import threading

    reader, writer = socket.socketpair()
    with reader, writer:
        reader.settimeout(2)

        class SlowExecutable(os.PathLike[str]):
            def __fspath__(self) -> str:
                reader.recv(1)
                return sys.executable

        # EOF releases every conversion of the same argument, not just the first.
        wake = threading.Timer(0.5, writer.close)
        with _watch_stalls(monkeypatch, patch_sleep=True, database_gate=database_gate) as fired:
            wake.start()
            try:
                subprocess.run(  # noqa: S603 - test PathLike always resolves to sys.executable
                    [SlowExecutable(), "-c", "pass"],
                    capture_output=True,
                    timeout=0.2,
                    check=True,
                )
                assert fired, "stall guard ignored subprocess argument conversion"
                assert "__fspath__" in fired[0]
            finally:
                wake.cancel()
                wake.join()


def test_stall_guard_fires_during_subprocess_stdin_flush(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """communicate's entry flush can block before it checks its deadline."""
    import os
    import subprocess
    import threading

    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        bufsize=8192,
    ) as child:
        assert child.stdin is not None
        fd = child.stdin.fileno()
        os.set_blocking(fd, False)
        try:
            with suppress(BlockingIOError):
                while True:
                    os.write(fd, b"x" * 65536)
        finally:
            os.set_blocking(fd, True)
        assert child.stdin.write(b"x") == 1  # Leave one byte in BufferedWriter.

        wake = threading.Timer(0.5, child.terminate)
        with _watch_stalls(monkeypatch, patch_sleep=True, database_gate=database_gate) as fired:
            wake.start()
            try:
                with pytest.raises(subprocess.TimeoutExpired):
                    child.communicate(timeout=0.01)
                assert fired, "stall guard ignored subprocess stdin.flush"
                assert "_communicate" in fired[0]
            finally:
                wake.cancel()
                wake.join()
                child.kill()
                child.wait(timeout=2)


def test_stall_guard_fires_during_subprocess_argument_selector(
    monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """A selector in argument conversion is not communicate's selector wait."""
    import os
    import selectors
    import socket
    import subprocess
    import threading

    reader, writer = socket.socketpair()
    with reader, writer, selectors.DefaultSelector() as selector:
        selector.register(reader, selectors.EVENT_READ)

        class SlowExecutable(os.PathLike[str]):
            def __fspath__(self) -> str:
                selector.select(timeout=2)
                return sys.executable

        # EOF makes every conversion ready; the timeout bounds a broken timer.
        wake = threading.Timer(0.5, writer.close)
        with _watch_stalls(monkeypatch, patch_sleep=True, database_gate=database_gate) as fired:
            wake.start()
            try:
                subprocess.run(  # noqa: S603 - test PathLike always resolves to sys.executable
                    [SlowExecutable(), "-c", "pass"],
                    capture_output=True,
                    timeout=0.2,
                    check=True,
                )
                assert fired, "stall guard ignored a selector in subprocess argument conversion"
            finally:
                wake.cancel()
                wake.join()
