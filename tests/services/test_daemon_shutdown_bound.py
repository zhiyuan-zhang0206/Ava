"""Real child-process regressions: six daemons exit within a small bound of
SIGTERM even with a default-executor job in flight.

2026-09-18 (task #3940): the PITR base-candidate daemon's smooth stop SIGTERM'd
it while a routine multi-minute ``asyncio.to_thread`` scan was in flight.
``asyncio.run``'s close awaits ``shutdown_default_executor``, capped by CPython
at ``THREAD_JOIN_TIMEOUT`` (300 s) — the stop flow's entire budget
(`PAUSE_TIMEOUT_SECONDS`) — and interpreter teardown then joins a still-running
worker with no bound at all (measured: a ``shutdown(wait=False)`` worker is
still joined at exit). The exit lost that race by ~2 s and the unit stood
half-stopped until the watchdog respawned it. Task #4222's sweep-A migrated
this daemon family — watchdog, memory_indexer, heartbeat, page_server,
events_maintenance, memory_search — to the sibling daemons' established shape
(``asyncio.Runner`` + an explicit cancellation drain + a hard exit that skips
teardown; see ``services/agent_ops/daemon.py``, ``services/pitr/uploader_daemon.py``),
and these regressions lock the property in per daemon.

Each child runs the production ``main()`` — signal wiring included — with
``run()`` swapped for a wedge-shaped surrogate: one default-executor job that
never finishes (that daemon's in-flight check / reconcile / scan, which is what
holds the loop's executor at SIGTERM) plus a cancellable main task whose
``finally`` records that the explicit drain ran. The parent asserts the bounded
exit and the cleanup marker, with its own kill deadline so the old unbounded
shape fails the test instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import pytest

from ops.agent_pause import PAUSE_TIMEOUT_SECONDS

_REPO_ROOT = Path(__file__).resolve().parents[2]

_MARKERS_ENV = "DAEMON_SHUTDOWN_TEST_MARKERS"
_CASE_ENV = "DAEMON_SHUTDOWN_TEST_CASE"

# The child must exit within this bound of SIGTERM — small enough to separate a
# bounded exit from a join-stalled one, and far below the stop budget the bound
# exists to protect. _KILL_SLACK_S sits above it so the old shape surfaces as a
# failed assertion carrying the child's log, never as a bare pytest hang.
_EXIT_BOUND_S = 10.0
_KILL_SLACK_S = 15.0

# Long enough to outlive any harness deadline: the daemon must exit regardless
# of the job still running.
_WEDGE_SECONDS = 600.0


@dataclass(frozen=True)
class _DaemonCase:
    """One daemon under the family-wide bounded-exit contract.

    ``interrupt_line`` is the stop log line its ``main()`` writes on the signal
    path (the daemon logs through stdlib ``logging``, so plain substrings).
    ``argv`` feeds daemons whose ``main()`` parses arguments (watchdog roles).
    """

    module: str
    interrupt_line: str
    argv: tuple[str, ...] = ()


_CASES: dict[str, _DaemonCase] = {
    "memory_indexer": _DaemonCase(
        module="services.memory_indexer.daemon",
        interrupt_line="[indexer] received interrupt, shutting down",
    ),
    "heartbeat": _DaemonCase(
        module="services.heartbeat.daemon",
        interrupt_line="[heartbeat] interrupted, shutting down",
    ),
    "page_server": _DaemonCase(
        module="services.page_server.daemon",
        interrupt_line="[page-server] interrupted, shutting down",
    ),
    "events_maintenance": _DaemonCase(
        module="services.events_maintenance.daemon",
        interrupt_line="[events-maintenance] interrupted, shutting down",
    ),
    "memory_search": _DaemonCase(
        module="services.memory_search.daemon",
        interrupt_line="[memory-search] interrupted, shutting down",
    ),
}


def _mark(name: str) -> None:
    """Append one marker line — the child-to-parent protocol."""
    with Path(os.environ[_MARKERS_ENV]).open("a", encoding="utf-8") as fh:
        fh.write(f"{name}\n")


def _run_child() -> None:
    """Child entry: production ``main()`` with a wedge-shaped ``run()``."""
    import importlib

    case = _CASES[os.environ[_CASE_ENV]]
    module: Any = importlib.import_module(case.module)
    if case.argv:
        sys.argv = [case.module, *case.argv]

    async def wedge_run(*_args: object) -> None:
        try:
            # The shape under test: a default-executor job that never finishes
            # (the in-flight check / scan / reconcile), so closing the loop can
            # only be bounded by refusing the executor join.
            asyncio.get_running_loop().run_in_executor(None, time.sleep, _WEDGE_SECONDS)
            _mark("wedge-scheduled")
            _mark("ready")
            while True:
                await asyncio.sleep(0.1)
        finally:
            _mark("cleanup-ran")

    module.run = wedge_run  # the signal path must still reach this task's cleanup
    module.main()


@dataclass
class _Child:
    proc: subprocess.Popen[bytes]
    markers_path: Path
    log_path: Path
    log_file: IO[bytes]
    terminated_at: float | None = None

    def markers(self) -> str:
        if not self.markers_path.exists():
            return ""
        return self.markers_path.read_text(encoding="utf-8")

    def log_tail(self, limit: int = 8000) -> str:
        if not self.log_path.exists():
            return "(no child output)"
        return self.log_path.read_text(encoding="utf-8", errors="replace")[-limit:]

    def wait_marker(self, name: str, *, timeout_s: float = 90.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if name in self.markers():
                return
            if self.proc.poll() is not None:
                pytest.fail(
                    f"daemon child exited before marker {name!r} "
                    f"(rc={self.proc.returncode}):\n{self.log_tail()}"
                )
            time.sleep(0.05)
        self.kill()
        pytest.fail(f"daemon child never wrote marker {name!r}:\n{self.log_tail()}")

    def terminate(self) -> None:
        self.terminated_at = time.monotonic()
        self.proc.send_signal(signal.SIGTERM)

    def wait_bounded_exit(self, *, what: str) -> float:
        assert self.terminated_at is not None
        try:
            rc = self.proc.wait(timeout=_EXIT_BOUND_S + _KILL_SLACK_S)
        except subprocess.TimeoutExpired:
            self.kill()
            pytest.fail(
                f"daemon child did not exit within {_EXIT_BOUND_S + _KILL_SLACK_S:.0f}s of "
                f"SIGTERM ({what}) — teardown is joining the executor again. "
                f"Markers: {self.markers()!r}\nchild log tail:\n{self.log_tail()}"
            )
        elapsed = time.monotonic() - self.terminated_at
        assert rc in (0, -signal.SIGTERM), (
            f"daemon child exited rc={rc} ({what}):\n{self.log_tail()}"
        )
        assert elapsed < _EXIT_BOUND_S, (
            f"daemon child took {elapsed:.1f}s to exit after SIGTERM ({what}); "
            f"bound expected {_EXIT_BOUND_S:.0f}s"
        )
        return elapsed

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            with suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=5)

    def close(self) -> None:
        self.kill()
        self.log_file.close()


def _spawn_child(tmp_path: Path, slug: str) -> _Child:
    markers_path = tmp_path / "markers.txt"
    log_path = tmp_path / "child.log"
    # The child inherits the suite's provisioned $AVA_HOME (machine identity,
    # .env-declared cluster keys, live URL values): a fresh bare home would be
    # treated as a not-yet-installed unit by the boot authority pass, which
    # drops the cluster-scope env keys and plants the unanchored sentinel.
    env = os.environ.copy()
    env[_MARKERS_ENV] = str(markers_path)
    env[_CASE_ENV] = slug
    log_file = log_path.open("wb")
    proc = subprocess.Popen(  # noqa: S603 -- fixed interpreter + in-repo entry, no shell
        [sys.executable, "-c", f"from {__name__} import _run_child; _run_child()"],
        cwd=_REPO_ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    child = _Child(proc=proc, markers_path=markers_path, log_path=log_path, log_file=log_file)
    child.wait_marker("ready")
    return child


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX SIGTERM path; Windows stops route through the private console",
)
@pytest.mark.parametrize("slug", sorted(_CASES))
def test_sigterm_bounded_exit_with_wedged_executor(tmp_path: Path, slug: str) -> None:
    """SIGTERM exits within the bound while a wedged executor job is in flight."""
    # The exit bound only matters relative to the stop budget it protects:
    # assert the relationship, not just the number.
    assert _EXIT_BOUND_S + _KILL_SLACK_S < PAUSE_TIMEOUT_SECONDS / 5
    case = _CASES[slug]
    child = _spawn_child(tmp_path, slug)
    try:
        child.terminate()
        child.wait_bounded_exit(what=f"{slug} with a wedged executor job")
        assert case.interrupt_line in child.log_tail(), child.log_tail()
        assert "cleanup-ran" in child.markers(), (
            "the cancellation drain did not reach run()'s cleanup"
        )
    finally:
        child.close()
