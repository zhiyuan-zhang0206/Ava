"""Shared child-process harness: a daemon exits bounded on SIGTERM (task #4224).

Each daemon under test runs its production ``main()`` in a real subprocess with
``run()`` swapped for a wedge-shaped surrogate: one default-executor job that
never finishes — so closing the loop can only be bounded by refusing the
executor join — plus a cancellable main task whose ``finally`` records that the
explicit cancellation drain ran. The parent sends SIGTERM, asserts the exit
lands within a small bound and that the drain reached ``run()``'s cleanup, and
carries its own kill deadline so the old unbounded shape fails as an assertion
instead of hanging the suite.

The shape was proven by the pitr base-candidate regression (task #4218, PR
#3045); this module hosts it for the sweep-B daemons and carries that
regression too (task #4239). The child stubs ``main()``'s boot gates that would
need a live cluster (the schema version check); everything from signal wiring
down is production code. A caller whose test must observe a precondition before
the stop window opens hands ``spawn_child`` a ``pre_ready`` callable: the child
runs it once the wedge is armed, before it signals ``ready``.

Reusers must keep both assertions — the ``interrupted`` log line and the
``cleanup-ran`` marker: a bounded exit alone would pass even if the drain
silently stopped reaching ``run()``'s cleanup.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_MARKERS_ENV = "DAEMON_SHUTDOWN_TEST_MARKERS"
_CHILD_MODULE_ENV = "DAEMON_SHUTDOWN_TEST_MODULE"
_PRE_READY_ENV = "DAEMON_SHUTDOWN_TEST_PRE_READY"
_ENDPOINT_ENV = "AVA_TELEMETRY_OTLP_ENDPOINT"

# The child must exit within this bound of SIGTERM — small enough to separate a
# bounded exit from a join-stalled one, and far below the stop budget the bound
# exists to protect. KILL_SLACK_S sits above it so the old shape surfaces as a
# failed assertion carrying the child's log, never as a bare pytest hang.
EXIT_BOUND_S = 10.0
KILL_SLACK_S = 15.0

# Long enough to outlive any harness deadline: the daemon must exit regardless
# of the job still running.
WEDGE_SECONDS = 600.0


def _mark(name: str) -> None:
    """Append one marker line — the child-to-parent protocol."""
    with Path(os.environ[_MARKERS_ENV]).open("a", encoding="utf-8") as fh:
        fh.write(f"{name}\n")


def _load_pre_ready_probe() -> Callable[[], None] | None:
    """Resolve the optional parent-named probe the child runs before ``ready``."""
    spec = os.environ.get(_PRE_READY_ENV)
    if not spec:
        return None
    module_name, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def _noop_schema_gate(*_args: object, **_kwargs: object) -> None:
    """Stand-in for ``assert_schema_current``: the check needs a live cluster."""


def run_child() -> None:
    """Child entry: production ``main()`` with a wedge-shaped ``run()``."""
    import shared.migrations

    mod = importlib.import_module(os.environ[_CHILD_MODULE_ENV])
    pre_ready = _load_pre_ready_probe()

    # Orthogonal to the exit shape under test and would otherwise dial the
    # cluster DB; the subprocess stays hermetic from here down everything is
    # production code.
    shared.migrations.assert_schema_current = _noop_schema_gate

    async def wedge_run() -> None:
        try:
            # The shape under test: a default-executor job that never finishes
            # (any in-flight threadpool work at stop), so closing the loop can
            # only be bounded by refusing the executor join.
            asyncio.get_running_loop().run_in_executor(None, time.sleep, WEDGE_SECONDS)
            _mark("wedge-scheduled")
            if pre_ready is not None:
                # The caller's precondition (a dead OTLP window, say) must be
                # observable before the parent signals; run it here, after the
                # wedge is armed and before `ready` releases the parent.
                pre_ready()
            _mark("ready")
            while True:
                await asyncio.sleep(0.1)
        finally:
            _mark("cleanup-ran")

    mod.run = wedge_run  # type: ignore[attr-defined]  # the signal path must still reach this task's cleanup
    mod.main()


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

    def wait_bounded_exit(self, *, what: str) -> float:
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
        assert rc in (0, -signal.SIGTERM), (
            f"daemon child exited rc={rc} ({what}):\n{self.log_tail()}"
        )
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
    module: str,
    label: str,
    pre_ready: Callable[[], None] | None = None,
) -> Child:
    """Spawn ``module``'s production main() with a wedge run(); wait for ready.

    ``pre_ready`` — an importable module-level callable — runs in the child
    between arming the wedge and signalling ``ready``, for a precondition the
    test must observe before the parent signals.
    """
    dead_port = _free_port()
    endpoint = f"http://127.0.0.1:{dead_port}"
    markers_path = tmp_path / "markers.txt"
    log_path = tmp_path / f"{label}.log"
    # A throwaway home keeps every home-derived path (pidfiles, config chain)
    # inside tmp_path; the dead OTLP endpoint keeps telemetry export from
    # dialing the operator's real collector.
    home = tmp_path / "ava-home"
    home.mkdir()
    (home / ".env").write_text(f"{_ENDPOINT_ENV}={endpoint}\n", encoding="utf-8")
    (home / "machine_name").write_text(f"{label}-shutdown-test\n", encoding="utf-8")
    env = os.environ.copy()
    env["AVA_HOME"] = str(home)
    env[_MARKERS_ENV] = str(markers_path)
    env[_CHILD_MODULE_ENV] = module
    env[_ENDPOINT_ENV] = endpoint
    if pre_ready is not None:
        env[_PRE_READY_ENV] = f"{pre_ready.__module__}:{pre_ready.__qualname__}"
    else:
        env.pop(_PRE_READY_ENV, None)
    # The helper-chain guard acts on this marker; a test child is not
    # helper-spawned, and inheriting a stray marker would self-exit(70).
    env.pop("AVA_PERMISSIONS_HELPER_PID", None)
    log_file = log_path.open("wb")
    proc = subprocess.Popen(  # fixed interpreter + in-repo entry, no shell
        [
            sys.executable,
            "-c",
            "from tests.services.daemon_shutdown_test_support import run_child; run_child()",
        ],
        cwd=_REPO_ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    child = Child(proc=proc, markers_path=markers_path, log_path=log_path, log_file=log_file)
    child.wait_marker("ready")
    return child
