"""Operator and updater stop semantics, with private real process boundaries."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import psutil
import pytest

from cli.commands import _temporary_stop as command
from cli.commands import stop as entry
from cli.commands._maintenance_stop import OwnedProcess
from cli.commands._pause_resume import resume_after_start
from cli.parsers import build_parser
from ops import agent_pause
from shared import maintenance, pause_owner, start_serving
from shared.maintenance_state import MaintenanceHold
from shared.session_backend import PtySessionBackend
from tests.agent.test_maintenance import WHEN
from tests.agent.test_maintenance import isolate as isolate
from tests.cli.test_maintenance_stop import Launcher
from tests.cli.test_maintenance_stop import home as home
from tests.cli.test_maintenance_stop import launch as launch

_NORMAL = "import signal,sys,time\nsignal.signal(signal.SIGTERM,lambda *_:sys.exit(0))\nprint('ready',flush=True)\nwhile True:time.sleep(.02)"
_IGNORE = "import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nprint('ready',flush=True)\nwhile True:time.sleep(.02)"


def drained() -> None:
    pause_owner.begin_maintenance("local", WHEN)
    pause_owner.change_maintenance("local", WHEN, MaintenanceHold(), MaintenanceHold("drained"))


def dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command, "pause_agents", lambda _timeout: drained())  # pyright: ignore[reportUnknownArgumentType]
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
    monkeypatch.setattr("shared.proc.hosting_supervised_session", lambda: None)
    monkeypatch.setattr("shared.host_deploy_state.set_posture", lambda _value: None)  # pyright: ignore[reportUnknownArgumentType]


def test_retired_service_failure_prevents_native_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    dependencies(monkeypatch)
    retired = MagicMock(side_effect=TimeoutError("retired service is still running"))
    pause = MagicMock()
    monkeypatch.setattr(command, "stop_retired_services", retired)
    monkeypatch.setattr(command, "pause_agents", pause)
    assert entry.cmd_pause(timeout=1) == 1
    retired.assert_called_once()
    pause.assert_not_called()


def test_real_service_pause_preserves_unselected_orchestration_and_pty(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies(monkeypatch)
    # Bootstrap and spawned interpreters consume the raw home before Settings.
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setenv("AVA_HOME_OVERRIDE", "1")
    monkeypatch.setenv("HOME", str(home))
    service, orchestration = launch("ava-worker", _NORMAL), launch("ava-rollout", _IGNORE)
    terminal = PtySessionBackend()
    name = "ava-agent-987-shell-1"
    assert terminal.new_session(name, "", home, env={"AVA_HOME": str(home)})
    from shared.pty_sessions._paths import record_path
    from shared.session_record import SessionRecord

    record = SessionRecord.read(record_path(name))
    assert record is not None
    identity = OwnedProcess(record.pid, record.create_time, record.starttime)
    deadline = time.monotonic() + 5
    while psutil.Process(record.pid).children(recursive=True):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    try:
        assert entry.cmd_pause(timeout=5) == 0
        assert service.poll() is not None
        assert orchestration.poll() is None
        assert terminal.has_session(name) and identity.live()
        current = maintenance.snapshot()
        assert current is not None and current.maintenance is not None
        assert current.maintenance.phase == "stopped"
    finally:
        terminal.kill_session(name)


def test_real_stubborn_service_fails_without_force_and_keeps_hold(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies(monkeypatch)
    service = launch("ava-worker", _IGNORE)
    before = psutil.Process(service.pid).create_time()
    assert entry.cmd_pause(timeout=0.2) == 1
    assert service.poll() is None
    assert psutil.Process(service.pid).create_time() == before
    assert maintenance.held()


def test_full_stop_closes_real_idle_terminal_after_drain(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies(monkeypatch)
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setenv("AVA_HOME_OVERRIDE", "1")
    monkeypatch.setenv("HOME", str(home))
    from cli.commands import _maintenance_stop as strict

    terminal = PtySessionBackend()
    monkeypatch.setattr(command, "get_shell_backend", lambda: terminal)
    monkeypatch.setattr(strict, "get_shell_backend", lambda: terminal)
    for name in ("stop_gate_service", "stop_permissions_helper", "stop_lgtm_services"):
        monkeypatch.setattr(f"cli.commands._stop_extras.{name}", lambda **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(entry, "_announce_stopping", lambda: None)
    name = "ava-agent-987-shell-2"
    assert terminal.new_session(name, "", home, env={"AVA_HOME": str(home)})
    try:
        assert entry.cmd_stop(require_confirmation=False, keep_infra=True, timeout=5) == 0
        assert not terminal.has_session(name)
    finally:
        terminal.kill_session(name)


@pytest.mark.real_cluster_spawn
def test_normal_start_releases_hold_only_after_successful_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drained()
    monkeypatch.setattr("ops.cluster_pause._unpause_local_cluster", lambda: None)
    monkeypatch.setattr(agent_pause, "_wake", lambda _hold: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)

    @resume_after_start
    def start(result: int) -> int:
        maintenance.require_start_allowed()
        assert maintenance.held()
        return result

    assert start(4) == 4
    assert maintenance.held()
    assert start(0) == 0
    assert not maintenance.held()


def test_plain_start_and_parser_need_no_manual_operation() -> None:
    @resume_after_start
    def start() -> int:
        assert not maintenance.held()
        return 0

    assert start() == start() == 0
    parser = build_parser()
    pause = parser.parse_args(["pause", "--keep-service", "frontend"])
    stop = parser.parse_args(["stop", "--keep-infra", "--keep-service", "gateway", "--force"])
    assert pause.keep_service == ["frontend"] and not pause.force
    assert stop.keep_infra and stop.force and stop.stop_browser


def test_only_explicit_force_enters_legacy_force_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    normal, force = MagicMock(return_value=0), MagicMock(return_value=0)
    monkeypatch.setattr(command, "stop", normal)
    monkeypatch.setattr(entry, "_force_stop", force)
    assert entry._do_stop(Path("/unused"), graceful=False, force_reap_agents=True) == 0
    normal.assert_called_once()
    force.assert_not_called()
    assert entry.cmd_stop(force=True, require_confirmation=False) == 0
    force.assert_called_once()


def test_repeated_stop_needs_no_live_database_or_host(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dependencies(monkeypatch)
    drained()
    maintenance.set_phase("local", WHEN, "stopping")
    maintenance.set_phase("local", WHEN, "stopped")
    monkeypatch.setattr(command, "pause_agents", agent_pause.pause_agents)
    monkeypatch.setattr(
        agent_pause, "host_identity", MagicMock(side_effect=AssertionError("host is down"))
    )
    monkeypatch.setattr(agent_pause, "connect", MagicMock(side_effect=AssertionError("DB is down")))
    monkeypatch.setattr(
        "shared.host_deploy_state.set_posture", MagicMock(side_effect=AssertionError("DB is down"))
    )
    assert entry.cmd_pause(timeout=1) == 0


def test_failed_stop_blocks_checkout_and_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import _update_local as local
    from cli.commands import update
    from shared.exit_codes import STOP_INCOMPLETE_EXIT_CODE

    monkeypatch.setattr(update, "_do_stop", MagicMock(return_value=1))
    checkout, boot = MagicMock(), MagicMock()
    monkeypatch.setattr(local, "_checkout_and_sync", checkout)
    monkeypatch.setattr(local, "_boot_gateway_fresh", boot)
    assert (
        local._run_gateway_local_update(
            Path("/unused"), target_sha="new", pull_recover=("old", set(), None), pull=True
        )
        == STOP_INCOMPLETE_EXIT_CODE
    )
    checkout.assert_not_called()
    boot.assert_not_called()


@pytest.mark.real_cluster_spawn
def test_failed_flush_cannot_be_released_by_a_healthy_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drained()
    current = maintenance.require_operation("local", WHEN)
    assert current.maintenance is not None
    failed = MaintenanceHold("draining", commands={42: 7}, failures={42: "final flush failed"})
    pause_owner.change_maintenance("local", WHEN, current.maintenance, failed)
    start = MagicMock(return_value=0)
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    with pytest.raises(RuntimeError, match="failed continuation/flush"):
        resume_after_start(start)()
    start.assert_not_called()
    from ops.cluster_pause import unpause_local_cluster

    with pytest.raises(RuntimeError, match="failed continuation/flush"):
        unpause_local_cluster()
    with pytest.raises(RuntimeError, match="failed continuation/flush"):
        agent_pause.resume_agents()
    assert maintenance.held()


@pytest.mark.real_cluster_spawn
def test_two_pause_start_cycles_reuse_identity_not_old_operation(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dependencies(monkeypatch)
    monkeypatch.setattr(command, "pause_agents", agent_pause.pause_agents)
    monkeypatch.setattr(agent_pause, "machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(agent_pause, "machine_name", lambda: "test-machine")
    monkeypatch.setattr("ops.cluster_pause._unpause_local_cluster", MagicMock())
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    starts = resume_after_start(lambda: 0)
    holders: list[str | None] = []
    for _ in range(2):
        assert entry.cmd_pause(timeout=3) == 0
        holders.append(pause_owner.read().holder)
        assert maintenance.held()
        assert starts() == 0
        assert not maintenance.held()
    assert holders[0] != holders[1]


@pytest.mark.real_cluster_spawn
def test_resource_stop_excludes_concurrent_start(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event, Thread

    from cli.commands._pause_resume import exclusive_resources

    entered, finish = Event(), Event()

    @exclusive_resources
    def stopping() -> None:
        entered.set()
        assert finish.wait(5)

    thread = Thread(target=stopping)
    thread.start()
    assert entered.wait(5)
    start = MagicMock(return_value=0)
    try:
        with pytest.raises(RuntimeError, match="lock"):
            resume_after_start(start)()
        start.assert_not_called()
    finally:
        finish.set()
        thread.join(5)
    assert not thread.is_alive()


@pytest.mark.parametrize("full_stop", [False, True])
def test_explicit_force_stops_host_and_preserves_only_pause_terminals(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    full_stop: bool,
) -> None:
    from cli import commands
    from shared.session_backend import PosixProcSessionBackend

    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setenv("AVA_HOME_OVERRIDE", "1")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("shared.session_backend.get_backend", PosixProcSessionBackend)
    monkeypatch.setattr("shared.session_backend.get_shell_backend", PtySessionBackend)
    monkeypatch.setattr(commands, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(entry, "build_services", lambda: [SimpleNamespace(session="agent-host")])
    monkeypatch.setattr(entry, "_announce_stopping", lambda: None)
    monkeypatch.setattr(entry, "_reap_orphan_step", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    for name in ("stop_gate_service", "stop_permissions_helper", "stop_lgtm_services"):
        monkeypatch.setattr(f"cli.commands._stop_extras.{name}", lambda **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        command, "pause_agents", MagicMock(side_effect=AssertionError("force fabricated a drain"))
    )
    host = launch("ava-agent-host", _IGNORE)
    terminal = PtySessionBackend()
    name = "ava-agent-987-shell-force"
    assert terminal.new_session(name, "", home, env={"AVA_HOME": str(home)})
    try:
        if full_stop:
            rc = entry.cmd_stop(force=True, require_confirmation=False, stop_browser=False)
        else:
            rc = entry.cmd_pause(force=True)
        assert rc == 0
        # The backend reaps its tracked process, so Popen cannot retain its
        # signal exit code. Verify the original TERM-ignoring PID is gone.
        host.wait(timeout=2)
        assert not psutil.pid_exists(host.pid)
        assert terminal.has_session(name) is not full_stop
        assert not maintenance.held(), "force must not invent a durable flush receipt"
    finally:
        terminal.kill_session(name)
