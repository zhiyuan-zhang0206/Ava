"""Real child-process regression: the base-candidate daemon exits within a small
bound of SIGTERM even with a default-executor job mid-flight and the OTLP
endpoint in a dead window.

2026-09-18 (task #3940): the wsl unit's smooth stop SIGTERM'd this daemon while
its retention refresh — a routine multi-minute ``asyncio.to_thread`` scan
(#3345) — was in flight. ``asyncio.run``'s close awaits
``shutdown_default_executor``, capped by CPython at ``THREAD_JOIN_TIMEOUT``
(300 s) — the stop flow's entire budget (`PAUSE_TIMEOUT_SECONDS`). The exit
lost that race by ~2 s (stop judged incomplete and the unit stood half-stopped
for ~6.5 min until the watchdog respawn), and interpreter teardown then joins a
still-running worker with no bound at all. The daemon now hard-exits after an
explicit cancellation drain and never joins the executor.

The child runs the production ``main()`` — signal wiring included — with
``run()`` swapped for a wedge-shaped surrogate: one default-executor job that
never finishes plus a cancellable main task whose ``finally`` records that the
explicit drain ran. The parent asserts the bounded exit and the cleanup marker,
with its own kill deadline so the old unbounded shape fails the test instead of
hanging the suite. The dead OTLP window rides the operator's real channels (a
throwaway ``$AVA_HOME`` and the process environment): the exporter's failed
bring-up must be observed before the signal, or the test would not reproduce
the incident's combination.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest

from ops.agent_pause import PAUSE_TIMEOUT_SECONDS

_REPO_ROOT = Path(__file__).resolve().parents[2]

_MARKERS_ENV = "PITR_SHUTDOWN_TEST_MARKERS"
_ENDPOINT_ENV = "AVA_TELEMETRY_OTLP_ENDPOINT"

# The child must exit within this bound of SIGTERM — small enough to separate a
# bounded exit from a join-stalled one, and far below the stop budget the bound
# exists to protect. _KILL_SLACK_S sits above it so the old shape surfaces as a
# failed assertion carrying the child's log, never as a bare pytest hang.
_EXIT_BOUND_S = 10.0
_KILL_SLACK_S = 15.0

# Long enough to outlive any harness deadline: the daemon must exit regardless
# of the job still running.
_WEDGE_SECONDS = 600.0


def _mark(name: str) -> None:
    """Append one marker line — the child-to-parent protocol."""
    with Path(os.environ[_MARKERS_ENV]).open("a", encoding="utf-8") as fh:
        fh.write(f"{name}\n")


def _emit_and_probe() -> None:
    """Drive the OTLP bring-up synchronously so its failure is observable."""
    from shared import telemetry

    telemetry.emit(
        "telemetry",
        "pitr_remote_inventory",
        attributes={
            "backend": "test",
            "object_count": 0,
            "bytes": 0,
            "logical_object_count": 0,
            "logical_bytes": 0,
        },
    )
    telemetry.flush()


def _run_child() -> None:
    """Child entry: production ``main()`` with a wedge-shaped ``run()``."""
    import services.pitr.base_scheduler_daemon as mod

    async def wedge_run() -> None:
        try:
            # The shape under test: a default-executor job that never finishes
            # (the in-flight retention scan), so closing the loop can only be
            # bounded by refusing the executor join.
            asyncio.get_running_loop().run_in_executor(None, time.sleep, _WEDGE_SECONDS)
            _mark("wedge-scheduled")
            _emit_and_probe()
            _mark("ready")
            while True:
                await asyncio.sleep(0.1)
        finally:
            _mark("cleanup-ran")

    mod.run = wedge_run  # the signal path must still reach this task's cleanup
    mod.main()


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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _spawn_child(tmp_path: Path) -> _Child:
    dead_port = _free_port()
    endpoint = f"http://127.0.0.1:{dead_port}"
    markers_path = tmp_path / "markers.txt"
    log_path = tmp_path / "child.log"
    # The dead endpoint rides both channels: the process environment (the
    # presence gate `endpoint_override_is_explicit` reads) and the throwaway
    # home's `.env` (the operator channel the settings chain trusts).
    home = tmp_path / "ava-home"
    home.mkdir()
    (home / ".env").write_text(f"{_ENDPOINT_ENV}={endpoint}\n", encoding="utf-8")
    # The boot recording (`service_started`) resolves the machine identity.
    (home / "machine_name").write_text("pitr-shutdown-test\n", encoding="utf-8")
    env = os.environ.copy()
    env["AVA_HOME"] = str(home)
    env[_MARKERS_ENV] = str(markers_path)
    env[_ENDPOINT_ENV] = endpoint
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
def test_sigterm_bounded_exit_with_wedged_executor_and_dead_otlp(tmp_path: Path) -> None:
    """SIGTERM exits within the bound while a wedged executor job and a dead OTLP window stand."""
    # The exit bound only matters relative to the stop budget it protects:
    # assert the relationship, not just the number.
    assert _EXIT_BOUND_S + _KILL_SLACK_S < PAUSE_TIMEOUT_SECONDS / 5
    child = _spawn_child(tmp_path)
    try:
        log = child.log_tail()
        assert "not answering" in log, f"the dead OTLP window was never observed:\n{log}"
        child.terminate()
        child.wait_bounded_exit(what="wedged executor + dead OTLP window")
        assert "interrupted, shutting down" in child.log_tail(), child.log_tail()
        assert "cleanup-ran" in child.markers(), (
            "the cancellation drain did not reach run()'s cleanup"
        )
    finally:
        child.close()
