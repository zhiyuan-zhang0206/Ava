"""`services.pidfile` — shared daemon pidfile discipline (audit round 2, P1).

The two historical failure modes under test: pid reuse (a stale pidfile
whose pid was recycled makes every start misjudge "already running") and
the write race (two daemons both pass the guard, both write, both run).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ava_builtins.plugins.ava_fleet.task_maintenance import daemon as task_maintenance_daemon
from base.config import ConfigBoot
from base.host.proc import process_cmdline
from services import pidfile as pidfiles
from services.agent_runner.agent_ops import boot as ops_boot
from services.agent_runner.agent_ops import daemon as agent_ops_daemon
from services.agent_runner.page_server import daemon as page_server_daemon
from services.backup.scheduler import daemon as backup_scheduler_daemon
from services.derived.labeler import daemon as labeler_daemon
from services.derived.memory_indexer import daemon as memory_indexer_daemon
from services.entrypoints.im_bridge import daemon as im_bridge_daemon
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.upkeep.events_maintenance import daemon as events_maintenance_daemon
from services.wake.delivery_watchdog import daemon as delivery_watchdog_daemon
from services.wake.heartbeat import daemon as heartbeat_daemon


@pytest.mark.parametrize(
    "daemon",
    [
        labeler_daemon,
        heartbeat_daemon,
        backup_scheduler_daemon,
        page_server_daemon,
        im_bridge_daemon,
        memory_indexer_daemon,
        delivery_watchdog_daemon,
        events_maintenance_daemon,
        agent_ops_daemon,
        task_maintenance_daemon,
    ],
    ids=lambda module: module.__name__,
)
def test_daemon_guard_checks_only_current_pidfile(
    daemon: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    """Each singleton guard reads the path its daemon writes, exactly once."""
    module_name = daemon.__name__
    calls: list[tuple[Path, str]] = []

    def holds_daemon(path: Path, module: str) -> bool:
        calls.append((path, module))
        return False

    runtime = tmp_path / "run"
    if module_name == "services.agent_runner.page_server.daemon":
        resources = daemon.resources
        monkeypatch.setattr(resources.pidfiles, "pidfile_holds_daemon", holds_daemon)
        assert resources.is_running(process_config, runtime=runtime) is False
        assert calls == [(resources.pidfile(process_config, runtime=runtime), module_name)]
    elif module_name == "services.agent_runner.agent_ops.daemon":
        monkeypatch.setattr(pidfiles, "pidfile_holds_daemon", holds_daemon)
        assert ops_boot.is_running(process_config, runtime=runtime) is False
        assert calls == [(ops_boot.pidfile(process_config, runtime=runtime), module_name)]
    else:
        monkeypatch.setattr(daemon, "pidfile_holds_daemon", holds_daemon)
        if module_name in {
            "ava_builtins.plugins.ava_fleet.task_maintenance.daemon",
        }:
            assert daemon._is_running() is False
            assert calls == [(daemon._pidfile(), module_name)]
        else:
            assert daemon._is_running(process_config, runtime=runtime) is False
            assert calls == [(daemon._pidfile(process_config, runtime=runtime), module_name)]


def test_acquire_writes_own_pid(tmp_path: Any) -> None:
    pidfile = tmp_path / "svc.pid"
    assert acquire_pidfile(pidfile, "svc.daemon") is True
    assert pidfile.read_text().strip() == str(os.getpid())


def test_acquire_refuses_live_instance(tmp_path: Any) -> None:
    """A pidfile held by a live process running the same module is refused."""
    pidfile = tmp_path / "svc.pid"
    cmdline = process_cmdline(os.getpid())
    assert cmdline
    identity = " ".join(cmdline)
    assert acquire_pidfile(pidfile, identity) is True
    # second claim, same module, same live process -> refused, file untouched
    assert acquire_pidfile(pidfile, identity) is False
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
    cmdline = process_cmdline(os.getpid())
    assert cmdline
    identity = " ".join(cmdline)
    assert "services.svc.daemon" not in identity
    assert pidfile_holds_daemon(pidfile, "services.svc.daemon") is False
    assert pidfile_holds_daemon(pidfile, identity) is True


def test_remove_pidfile_idempotent(tmp_path: Any) -> None:
    pidfile = tmp_path / "svc.pid"
    pidfile.write_text("1\n")
    remove_pidfile(pidfile)
    assert not pidfile.exists()
    remove_pidfile(pidfile)  # must not raise
