"""Shared child-process harness: a daemon exits bounded on SIGTERM (task #4224).

Each daemon under test runs its production ``main()`` in a real subprocess with
``run()`` swapped for a wedge-shaped surrogate: one default-executor job that
never finishes — so closing the loop can only be bounded by refusing the
executor join — plus a cancellable main task whose ``finally`` records that the
explicit cancellation drain ran. The parent sends SIGTERM, asserts the exit
lands within a small bound and that the drain reached ``run()``'s cleanup, and
carries its own kill deadline so the old unbounded shape fails as an assertion
instead of hanging the suite.

The shape was first proven on a daemon with a multi-minute ``to_thread`` scan
(task #4218, PR #3045); this module hosts it for the sweep-B daemons (task
#4239). The child stubs ``main()``'s boot gates that would
need a live cluster (the schema version check); everything from signal wiring
down is production code. Each caller names its own statically imported daemon
in a child entry next to the daemon tests and passes that entry as a literal
command to ``spawn_child``.

Reusers must keep both assertions — the ``interrupted`` log line and the
``cleanup-ran`` marker: a bounded exit alone would pass even if the drain
silently stopped reaching ``run()``'s cleanup.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest

from base.host.tests.daemon_shutdown_child import (
    CLEANUP_FAILURE_ENV,
    INTERNAL_STOP_ENV,
    MARKERS_ENV,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

_ENDPOINT_ENV = "AVA_TELEMETRY_OTLP_ENDPOINT"

# The child must exit within this bound of SIGTERM — small enough to separate a
# bounded exit from a join-stalled one, and far below the stop budget the bound
# exists to protect. KILL_SLACK_S sits above it so the old shape surfaces as a
# failed assertion carrying the child's log, never as a bare pytest hang.
EXIT_BOUND_S = 10.0
KILL_SLACK_S = 15.0


@dataclass
class Child:
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

    def wait_bounded_exit(
        self, *, what: str, expected_codes: tuple[int, ...] = (0, -signal.SIGTERM)
    ) -> float:
        assert self.terminated_at is not None
        try:
            rc = self.proc.wait(timeout=EXIT_BOUND_S + KILL_SLACK_S)
        except subprocess.TimeoutExpired:
            self.kill()
            pytest.fail(
                f"daemon child did not exit within {EXIT_BOUND_S + KILL_SLACK_S:.0f}s of "
                f"SIGTERM ({what}) — teardown is joining the executor again. "
                f"Markers: {self.markers()!r}\nchild log tail:\n{self.log_tail()}"
            )
        elapsed = time.monotonic() - self.terminated_at
        assert rc in expected_codes, f"daemon child exited rc={rc} ({what}):\n{self.log_tail()}"
        assert elapsed < EXIT_BOUND_S, (
            f"daemon child took {elapsed:.1f}s to exit after SIGTERM ({what}); "
            f"bound expected {EXIT_BOUND_S:.0f}s"
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def spawn_child(
    tmp_path: Path,
    *,
    command: Sequence[str],
    label: str,
    internal_stop: bool = False,
    cleanup_failure: bool = False,
) -> Child:
    """Start the caller's literal child command and wait for its wedge to be ready."""
    dead_port = _free_port()
    endpoint = f"http://127.0.0.1:{dead_port}"
    markers_path = tmp_path / "markers.txt"
    log_path = tmp_path / f"{label}.log"
    # A throwaway home keeps every home-derived path (pidfiles, config chain)
    # inside tmp_path; the dead OTLP endpoint keeps telemetry export from
    # dialing the operator's real collector.
    home = tmp_path / "ava-home"
    home.mkdir()
    (home / ".env").write_text(
        f"{_ENDPOINT_ENV}={endpoint}\nAVA_MACHINE_NAME={label}-shutdown-test\n", encoding="utf-8"
    )
    env = os.environ.copy()
    env["AVA_HOME"] = str(home)
    env[MARKERS_ENV] = str(markers_path)
    env[_ENDPOINT_ENV] = endpoint
    env[INTERNAL_STOP_ENV] = "1" if internal_stop else "0"
    env[CLEANUP_FAILURE_ENV] = "1" if cleanup_failure else "0"
    # The helper-chain guard acts on this marker; a test child is not
    # helper-spawned, and inheriting a stray marker would self-exit(70).
    env.pop("AVA_PERMISSIONS_HELPER_PID", None)
    log_file = log_path.open("wb")
    proc = subprocess.Popen(  # noqa: S603 -- callers pass a literal repository test entry, no shell
        command,
        cwd=_REPO_ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    child = Child(proc=proc, markers_path=markers_path, log_path=log_path, log_file=log_file)
    child.wait_marker("ready")
    if internal_stop:
        child.terminated_at = time.monotonic()
    return child
