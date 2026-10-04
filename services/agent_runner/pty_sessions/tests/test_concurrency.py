"""Many sessions streaming output while new ones are forked, all in one process.

The service forks every shell from a request thread of a multi-threaded process.
The child does only what cannot wait on a lock another thread held at the fork
(`session.fork_shell`), and the proof is this load: twenty sessions printing
continuously, a few of them with a live screen model, while threads allocate more.
A fork child that deadlocks shows as a shell that never answers; a starved accept
loop shows as a slow ping, which is what the ownership probe times out on.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from base.native_process.os_platform import IS_WINDOWS
from base.sessions.pty import client
from tests.path_scoped import pty_shells as support
from tests.path_scoped.pty_service import pty_service as pty_service
from tests.path_scoped.pty_shells import new, type_line, wait_for

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="pty sessions are POSIX-only"),
    pytest.mark.usefixtures("pty_service"),
]

_STREAMING = 20
_WITH_SCREEN = 5
_THREADS = 8
_PER_THREAD = 5

# The ownership probe gives a ping three seconds; staying well inside it under load is the bar.
_PING_BUDGET_S = 1.5


def _start_streaming(home: Path) -> list[str]:
    streaming = [f"ava-stress-stream-{i}" for i in range(_STREAMING)]
    for name in streaming:
        new(name, home)
    for name in streaming[:_WITH_SCREEN]:
        # Builds the live pyte model while the shell is quiet: every byte printed from now on
        # feeds it. Built after the flood starts, the first capture would replay a full 2 MiB
        # ring through pure-Python pyte (seconds of CPU) against twenty spinning `yes` processes.
        client.capture(name, 20, scrollback=True)
    for name in streaming:
        type_line(name, "yes streaming-output")
    return streaming


class _Pinger:
    """Pings the service in a loop and keeps every latency."""

    def __init__(self) -> None:
        self.latencies: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop)

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            client.request("ping")
            self.latencies.append(time.monotonic() - started)
            time.sleep(0.02)

    def __enter__(self) -> _Pinger:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=30)


def _allocate_concurrently(home: Path) -> tuple[dict[str, bool], list[BaseException]]:
    created: dict[str, bool] = {}
    errors: list[BaseException] = []
    lock = threading.Lock()

    def allocate(worker: int) -> None:
        for index in range(_PER_THREAD):
            name = f"ava-stress-new-{worker}-{index}"
            try:
                result = new(name, home)
            except BaseException as exc:  # reported by the caller's assertion
                with lock:
                    errors.append(exc)
                return
            with lock:
                created[name] = result

    workers = [threading.Thread(target=allocate, args=(w,)) for w in range(_THREADS)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)
    assert not any(worker.is_alive() for worker in workers), "an allocation never returned"
    return created, errors


def test_new_sessions_fork_cleanly_while_other_sessions_stream_output(unit_home: Path) -> None:
    streaming = _start_streaming(unit_home)

    started = time.monotonic()
    with _Pinger() as pinger:
        created, errors = _allocate_concurrently(unit_home)
    elapsed = time.monotonic() - started

    assert not errors, errors
    assert len(created) == _THREADS * _PER_THREAD and all(created.values())
    worst = max(pinger.latencies)
    assert worst < _PING_BUDGET_S, f"ping stalled for {worst:.2f}s"

    # Every freshly forked shell is a working shell, not a child stuck between fork and exec.
    for name in created:
        type_line(name, f"echo alive-{name}")
    for name in created:
        support.output_until(name, f"alive-{name}")
    # The streaming sessions kept streaming through all of it.
    assert wait_for(lambda: "streaming-output" in client.capture(streaming[0], 5, scrollback=False))
    assert {s.name for s in client.list_sessions()} == set(streaming) | set(created)
    print(  # noqa: T201 — the measured shape of the load, shown with -s
        f"{len(created)} forks in {elapsed:.1f}s with {_STREAMING} streaming sessions; "
        f"ping max {worst * 1000:.0f} ms over {len(pinger.latencies)} pings"
    )
