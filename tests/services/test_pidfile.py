"""`services._pidfile` — shared daemon pidfile discipline (audit round 2, P1).

The two historical failure modes under test: pid reuse (a stale pidfile
whose pid was recycled makes every start misjudge "already running") and
the write race (two daemons both pass the guard, both write, both run).
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

import pytest

from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile


@pytest.mark.parametrize(
    "module_name",
    [
        "services.labeler.daemon",
        "services.heartbeat.daemon",
        "services.backup_scheduler.daemon",
        "services.page_server.daemon",
        "services.im_bridge.daemon",
        "services.memory_indexer.daemon",
        "services.delivery_watchdog.daemon",
        "services.events_maintenance.daemon",
        "services.agent_ops.daemon",
        "ava_builtins.plugins.ava_fleet.task_maintenance.daemon",
    ],
)
def test_daemon_guard_checks_only_current_pidfile(
    module_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each singleton guard reads the path its daemon writes, exactly once."""
    daemon = importlib.import_module(module_name)
    calls: list[tuple[Path, str]] = []

    def holds_daemon(path: Path, module: str) -> bool:
        calls.append((path, module))
        return False

    monkeypatch.setattr(daemon, "pidfile_holds_daemon", holds_daemon)
    assert daemon._is_running() is False
    assert calls == [(daemon._PIDFILE, module_name)]


def test_acquire_writes_own_pid(tmp_path: Any) -> None:
    pidfile = tmp_path / "svc.pid"
    assert acquire_pidfile(pidfile, "svc.daemon") is True
    assert pidfile.read_text().strip() == str(os.getpid())


def test_acquire_refuses_live_instance(tmp_path: Any) -> None:
    """A pidfile held by a live process running the same module is refused."""
    pidfile = tmp_path / "svc.pid"
    assert acquire_pidfile(pidfile, "pytest") is True  # our own argv contains pytest
    # second claim, same module, same live process -> refused, file untouched
    assert acquire_pidfile(pidfile, "pytest") is False
    assert pidfile.read_text().strip() == str(os.getpid())


def test_acquire_reclaims_stale_dead_pid(tmp_path: Any) -> None:
    pidfile = tmp_path / "svc.pid"
    pidfile.write_text("99999999\n")  # pid far above any live process
    assert pidfile_holds_daemon(pidfile, "svc.daemon") is False
    assert acquire_pidfile(pidfile, "svc.daemon") is True
    assert pidfile.read_text().strip() == str(os.getpid())


def test_acquire_reclaims_recycled_pid(tmp_path: Any) -> None:
    """Regression: a stale pidfile whose pid was recycled by an unrelated
    process (live, but argv does not name the daemon) must be reclaimed,
    not mistaken for a running instance."""
    pidfile = tmp_path / "svc.pid"
    pidfile.write_text(f"{os.getpid()}\n")  # live pid, unrelated argv
    assert pidfile_holds_daemon(pidfile, "svc.daemon") is False
    assert acquire_pidfile(pidfile, "svc.daemon") is True


def test_pidfile_holds_daemon_false_when_argv_mismatch(tmp_path: Any) -> None:
    pidfile = tmp_path / "svc.pid"
    pidfile.write_text(f"{os.getpid()}\n")
    # our argv names pytest, not the daemon module
    assert pidfile_holds_daemon(pidfile, "services.svc.daemon") is False
    assert pidfile_holds_daemon(pidfile, "pytest") is True


def test_remove_pidfile_idempotent(tmp_path: Any) -> None:
    pidfile = tmp_path / "svc.pid"
    pidfile.write_text("1\n")
    remove_pidfile(pidfile)
    assert not pidfile.exists()
    remove_pidfile(pidfile)  # must not raise
