"""Helpers of the stop tests: a private home, the processes a stop must leave alone, and the stop's collaborators stubbed.

Terminals live in the pty-sessions service. A test that needs real shells takes
the `pty_service` fixture (re-exported here) next to `home`: the service runs
under the same private home, so the stop under test dials it exactly as it dials
the unit's own. Without the fixture no service listens, which is the state of a
unit whose service is down. `stub_closure` stands in for the service's closure where
a test needs processes it may not signal (another user's), which no test can create
for real.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import psutil
import pytest

import cli.commands.lifecycle.root_driver as _root_driver_commands
from base.deploy.maintenance import pause_owner
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from base.native_process import pid_starttime_ticks
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import closure
from base.sessions.pty.paths import SERVICE_UNIT
from base.sessions.record import SessionRecord
from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle import root_driver
from cli.commands.lifecycle import service_stop as strict
from ops import pty_close_notices
from tests.components.agent.test_maintenance import WHEN
from tests.e2e.process_support import kill_group_if_alive
from tests.path_scoped import pty_jobs as jobs
from tests.path_scoped.pty_reaper import PtyReaper
from tests.path_scoped.pty_service import PtyServiceProcess as PtyServiceProcess
from tests.path_scoped.pty_service import pty_service as pty_service
from tests.path_scoped.pty_shells import output_until

Launcher = Callable[[str, str], subprocess.Popen[str]]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    monkeypatch.setattr(root_driver, "root_tree_selection", dict)
    monkeypatch.setattr(root_driver, "stop_root_service_tree", Mock(return_value=0))
    monkeypatch.setattr(_root_driver_commands, "stop_root_service_tree", Mock(return_value=0))
    monkeypatch.setattr(_root_driver_commands, "_root_tree_plan", Mock(return_value=[]))

    # Stop intentionally consumes the ambient override. Every independent test
    # CLI still needs its explicit private binding when the checkout currently
    # points at an isolated native-proof home: the PTY CLI children inherit it.
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def launch(home: Path) -> Iterator[Callable[[str, str], subprocess.Popen[str]]]:
    processes: list[subprocess.Popen[str]] = []

    def create(name: str, code: str) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", code],
            cwd=home,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        processes.append(proc)
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        SessionRecord(
            proc.pid,
            psutil.Process(proc.pid).create_time(),
            "private-test",
            str(home),
            time.time(),
            pid_starttime_ticks(proc.pid),
            pgid=os.getpgid(proc.pid),
        ).write(home / "run/sessions" / f"{name}.json")
        return proc

    yield create
    for proc in processes:
        # Test fixture cleanup alone may kill the exact private process group it
        # created, after the assertions prove strict stop left it alive.
        kill_group_if_alive(proc)
        proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


def drained() -> None:
    pause_owner.begin_maintenance("local", WHEN)
    pause_owner.change_maintenance(
        "local", WHEN, MaintenanceHold(), MaintenanceHold(MaintenancePhase.DRAINED)
    )


def dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_root_driver_commands, "stop_root_service_tree", lambda **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    monkeypatch.setattr(_root_driver_commands, "_root_tree_plan", lambda _preserve: [])  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    monkeypatch.setattr(command, "pause_agents", lambda _db, _bus, _timeout, **_kw: drained())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(command, "machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(
        command,
        "build_services",
        lambda: [
            SimpleNamespace(session="worker", requires_db=False),
            SimpleNamespace(session="browser", requires_db=False),
            SimpleNamespace(session=SERVICE_UNIT, requires_db=False),
        ],
    )
    monkeypatch.setattr(command, "ops_quiescent", lambda _timeout: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("base.host.proc.hosting_supervised_session", lambda: None)
    monkeypatch.setattr("base.deploy.state.host_deploy_state.set_posture", lambda _db, _value: None)  # pyright: ignore[reportUnknownArgumentType]


def record_root_stops(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stand-in for the root owner's service-tree stop: every call's keyword arguments, in order.

    The pty-sessions service is the real subprocess of the `pty_service` fixture,
    so the stand-in never stops it: what a stop asked of the root owner is what
    the test reads.
    """
    calls: list[dict[str, Any]] = []

    def record(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(_root_driver_commands, "stop_root_service_tree", record)
    return calls


WRITE_NOTICES = pty_close_notices.write_notices


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[pty_close_notices.ClosureNotice]:
    """Stand-in for the stop's database write: every notice it would write, in order."""
    notices: list[pty_close_notices.ClosureNotice] = []

    def record(
        _db: object, _bus: object, batch: Sequence[pty_close_notices.ClosureNotice], *, direct: bool
    ) -> list[tuple[pty_close_notices.ClosureNotice, Exception]]:
        del direct
        notices.extend(batch)
        return []

    monkeypatch.setattr(pty_close_notices, "write_notices", record)
    return notices


def stop_env(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """What a real `ava stop` of this home needs beyond `dependencies`: its raw home and no extras."""
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    for hook in ("stop_permissions_helper",):
        monkeypatch.setattr(f"cli.commands.lifecycle._stop_extras.{hook}", lambda **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands.lifecycle.stop._announce_stopping", lambda: None)


def busy_session(
    home: Path, name: str, source: str, reaper: PtyReaper, *, ready_line: str = "job-ready"
) -> tuple[psutil.Process, list[psutil.Process]]:
    """Wait for this new job's bare output marker; pin its native identities."""
    shell = jobs.create(name, home, source)
    reaper.track_session(name)
    output_until(name, ready_line)
    running = jobs.live_children(shell)
    assert running, f"the ready job of {name} is no longer running"
    reaper.track(*running)
    return shell, running


def identity_of(process: subprocess.Popen[str]) -> OwnedProcess:
    """The exact identity (pid and birth) of a process a test launched."""
    return OwnedProcess.capture(psutil.Process(process.pid))


def stub_closure(
    monkeypatch: pytest.MonkeyPatch, *outcomes: closure.Outcome
) -> list[tuple[float, float]]:
    """Make each stop's closure return the next of `outcomes`; record the (grace, kill) it was asked for."""
    asked: list[tuple[float, float]] = []
    pending = list(outcomes)

    def close(grace_s: float, kill_s: float) -> closure.Outcome:
        asked.append((grace_s, kill_s))
        return pending.pop(0) if len(pending) > 1 else pending[0]

    monkeypatch.setattr(strict, "_close_via_service", close)
    return asked


def closed_session(name: str, left: tuple[tuple[int, str], ...] = ()) -> closure.ClosedSession:
    """A busy session the closure verified gone (its shell is a long-dead identity)."""
    return closure.ClosedSession(name, OwnedProcess(2_000_000_000, 1.0, None), left)
