"""Helpers of the stop tests: a private home, the processes a stop must leave alone, and the stop's collaborators stubbed."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

import cli.commands.lifecycle.root_driver as _root_driver_commands
from base.deploy.maintenance import pause_owner
from base.deploy.maintenance.state import MaintenanceHold
from base.native_process import pid_starttime_ticks
from base.sessions.record import SessionRecord
from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle import root_driver
from cli.commands.lifecycle import service_stop as stop
from tests.agent.test_maintenance import WHEN
from tests.e2e._proc import kill_group_if_alive

Launcher = Callable[[str, str], subprocess.Popen[str]]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    monkeypatch.setattr(stop, "get_shell_backend", lambda: SimpleNamespace(list_sessions=list))
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
    pause_owner.change_maintenance("local", WHEN, MaintenanceHold(), MaintenanceHold("drained"))


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
        ],
    )
    monkeypatch.setattr(command, "ops_quiescent", lambda _timeout: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("base.host.proc.hosting_supervised_session", lambda: None)
    monkeypatch.setattr("base.deploy.state.host_deploy_state.set_posture", lambda _db, _value: None)  # pyright: ignore[reportUnknownArgumentType]
