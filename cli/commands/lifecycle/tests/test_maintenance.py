"""Local CLI phases must retain the hold across failures and explicit startup."""

import os
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from urllib.error import URLError
from uuid import uuid4

import pytest

from base.db import Database
from base.db.tests.fakes import patch_database
from base.deploy.lifecycle import start_serving
from base.deploy.lifecycle.start_serving import RootBirth
from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenanceHold
from base.events.live.bus import EventBus
from cli.commands.lifecycle import maintenance as command
from ops.agent_pause.probe import HostIdentity
from ops.agent_pause.probe import host_identity_or_none as real_host_identity_or_none
from tests.agent.test_maintenance import WHEN
from tests.agent.test_maintenance import isolate as isolate


@pytest.fixture(autouse=True)
def cli_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command, "machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(
        command, "host_identity_or_none", lambda: HostIdentity(uuid4(), frozenset())
    )
    patch_database(monkeypatch, connect=MagicMock())
    monkeypatch.setattr(command.cohort, "verify_drained", MagicMock())
    monkeypatch.setattr("ops.agent_pause.publish_inbound_wake", MagicMock())
    monkeypatch.setattr("base.deploy.state.host_deploy_state.set_posture", MagicMock())
    monkeypatch.setattr(command, "ops_quiescent", MagicMock())


def phase(value: str) -> None:
    before = pause_owner.begin_maintenance("local", WHEN).snapshot
    assert before.maintenance is not None
    hold = MaintenanceHold.decode({**before.maintenance.encode(), "phase": value})
    pause_owner.change_maintenance("local", WHEN, before.maintenance, hold)


def test_stop_failure_retains_generation_and_retry_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phase("drained")

    def timeout(_timeout: float, *, keep_terminals: bool = False) -> list[str]:
        assert not keep_terminals
        raise TimeoutError("owned process still alive")

    monkeypatch.setattr(command, "stop_services", timeout)
    with pytest.raises(TimeoutError, match="still alive"):
        command.stop("local", WHEN, 2, gateway_last=False)
    assert command._hold("local", WHEN).phase == "stopping"
    with pytest.raises(RuntimeError, match="cannot release"):
        admission.require_start_allowed()
    monkeypatch.setattr(command, "stop_services", MagicMock(return_value=[]))
    command.stop("local", WHEN, 2, gateway_last=False)
    assert command._hold("local", WHEN).phase == "stopped"


def test_gateway_last_is_required_before_any_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    phase("drained")
    monkeypatch.setattr(command, "machine_role", lambda: frozenset({"gateway"}))
    stop = MagicMock()
    monkeypatch.setattr(command, "stop_services", stop)
    with pytest.raises(RuntimeError, match="gateway-last"):
        command.stop("local", WHEN, 2, gateway_last=False)
    stop.assert_not_called()
    assert command._hold("local", WHEN).phase == "drained"


def test_start_keeps_hold_until_explicit_resume(
    serving_root: RootBirth,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
) -> None:
    phase("stopped")
    monkeypatch.setattr(start_serving, "state_path", lambda: tmp_path / "serving.json")

    def start(**kwargs: Any) -> int:
        admission.require_start_allowed()
        assert admission.held()
        assert kwargs == {"persist_services": False}
        generation = start_serving.begin_start()
        assert start_serving.mark_serving(generation, runtime=serving_root.runtime)
        return 0

    def unpause(_db: object, _bus: object) -> None:
        from ops.agent_pause import resume_agents

        admission.require_start_allowed()
        assert admission.held()
        resume_agents(database, event_bus)

    monkeypatch.setattr("cli.commands.lifecycle.start.cmd_start", start)
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)
    assert command._start("local", WHEN) == 0
    assert command._hold("local", WHEN).phase == "ready"
    with pytest.raises(RuntimeError, match="cannot release"):
        admission.require_start_allowed()
    command.resume("local", WHEN, cancel=False)
    assert not admission.held()
    assert pause_owner.read().status == "resumed"


def test_failed_dependency_resume_never_releases_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    phase("preparing")

    def unavailable() -> None:
        raise ConnectionError("dependencies unavailable")

    patch_database(monkeypatch, connect=unavailable)
    with pytest.raises(ConnectionError):
        command.resume("local", WHEN, cancel=True)
    assert command._hold("local", WHEN).phase == "preparing"


def test_resume_proceeds_with_absent_agent_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An independently proven absent host cannot have continuations; the
    cancel half proceeds and says the downgrade loudly."""
    phase("draining")
    unpause = MagicMock()
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)
    monkeypatch.setattr(command, "host_identity_or_none", real_host_identity_or_none)
    monkeypatch.setattr("ops.agent_pause.probe.host_running", lambda: False)
    monkeypatch.setattr("ops.agent_pause.probe.host_identity", _refused_probe)

    command.resume("local", WHEN, cancel=True)

    unpause.assert_called_once()
    assert "provably absent" in capsys.readouterr().err


def test_resume_still_refuses_an_unreadable_host_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running host must answer; unreadable evidence is not absence."""
    phase("draining")
    monkeypatch.setattr(command, "host_identity_or_none", real_host_identity_or_none)
    monkeypatch.setattr("ops.agent_pause.probe.host_running", lambda: True)

    def _wedged() -> None:
        raise URLError(TimeoutError("timed out"))

    monkeypatch.setattr("ops.agent_pause.probe.host_identity", _wedged)

    with pytest.raises(URLError):
        command.resume("local", WHEN, cancel=True)
    assert pause_owner.read().status == "paused"


def test_failed_start_remains_retryable_under_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    phase("stopped")
    monkeypatch.setattr("cli.commands.lifecycle.start.cmd_start", MagicMock(return_value=7))
    assert command._start("local", WHEN) == 7
    assert command._hold("local", WHEN).phase == "starting"

    with pytest.raises(RuntimeError, match="requires maintenance start"):
        command.resume("local", WHEN, cancel=False)


@pytest.mark.parametrize("value", ["stopping", "stopped", "starting", "ready"])
def test_cancel_cannot_bypass_stop_or_start_readiness(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    phase(value)
    unpause = MagicMock()
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)
    with pytest.raises(RuntimeError, match="cancel cannot bypass"):
        command.resume("local", WHEN, cancel=True)
    unpause.assert_not_called()
    assert command._hold("local", WHEN).phase == value


@pytest.mark.parametrize("value", ["preparing", "draining", "drained"])
def test_cancel_can_abandon_drain_before_service_stop(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    phase(value)
    from ops.agent_pause import resume_agents

    unpause = MagicMock(side_effect=resume_agents)
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)
    command.resume("local", WHEN, cancel=True)
    unpause.assert_called_once()
    assert not admission.held()


def test_keep_terminals_does_not_skip_drain_or_ops_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    phase("drained")
    stop = MagicMock()
    monkeypatch.setattr(command, "stop_services", stop)
    verify = MagicMock(side_effect=RuntimeError("drain incomplete"))
    monkeypatch.setattr(command.cohort, "verify_drained", verify)
    with pytest.raises(RuntimeError, match="drain incomplete"):
        command.stop("local", WHEN, 2, gateway_last=False, keep_terminals=True)
    stop.assert_not_called()
    verify.side_effect = None
    monkeypatch.setattr(command, "ops_quiescent", MagicMock(side_effect=TimeoutError("busy ops")))
    with pytest.raises(TimeoutError, match="busy ops"):
        command.stop("local", WHEN, 2, gateway_last=False, keep_terminals=True)
    stop.assert_not_called()
    assert command._hold("local", WHEN).phase == "stopping"


def test_data_plane_keep_still_requires_native_root_absence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from services.ava_root.singleton import acquire_instance_lock, release_instance_lock

    phase("stopped")
    monkeypatch.setattr(command, "machine_role", lambda: frozenset({"gateway"}))
    root = tmp_path / "root"
    monkeypatch.setattr("base.paths.root_run_dir", lambda: root)
    shutdown = MagicMock()
    monkeypatch.setattr(command, "stop_data_plane", shutdown)
    owner = acquire_instance_lock(root)
    try:
        with pytest.raises(RuntimeError):
            command._stop_data("local", WHEN, 2, gateway_last=True, keep_terminals=True)
    finally:
        release_instance_lock(owner)
    shutdown.assert_not_called()


@pytest.mark.parametrize("keep", [False, True])
def test_data_plane_terminal_assertion_only_bypasses_terminal_guard(
    keep: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    phase("stopped")
    monkeypatch.setattr(command, "machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("base.paths.root_run_dir", lambda: tmp_path / "root")
    terminals = MagicMock(side_effect=RuntimeError("live terminal"))
    shutdown = MagicMock(return_value=[])
    monkeypatch.setattr(command, "require_no_terminals", terminals)
    monkeypatch.setattr(command, "stop_data_plane", shutdown)
    if keep:
        command._stop_data("local", WHEN, 2, gateway_last=True, keep_terminals=True)
        terminals.assert_not_called()
        shutdown.assert_called_once_with(2)
    else:
        with pytest.raises(RuntimeError, match="live terminal"):
            command._stop_data("local", WHEN, 2, gateway_last=True)
        shutdown.assert_not_called()


def failed_hold(*failures: int, phase_value: str = "draining") -> None:
    before = pause_owner.begin_maintenance("local", WHEN).snapshot
    assert before.maintenance is not None
    hold = MaintenanceHold.decode(
        {
            **before.maintenance.encode(),
            "phase": phase_value,
            "failures": {str(agent): "RuntimeError" for agent in failures},
        }
    )
    pause_owner.change_maintenance("local", WHEN, before.maintenance, hold)


def test_repair_refuses_without_failed_receipts() -> None:
    phase("draining")
    with pytest.raises(RuntimeError, match="no failed receipts to repair"):
        command._repair("local", WHEN, operator=None)


def test_repair_refuses_past_drain_phases() -> None:
    failed_hold(7, phase_value="stopped")
    with pytest.raises(RuntimeError, match="repair cannot bypass a started stop"):
        command._repair("local", WHEN, operator=None)


def test_repair_refuses_while_agent_host_has_active_continuations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_hold(7)
    monkeypatch.setattr(
        command, "host_identity_or_none", lambda: HostIdentity(uuid4(), frozenset({7}))
    )
    with pytest.raises(RuntimeError, match="still has active continuations"):
        command._repair("local", WHEN, operator=None)


def test_repair_moves_failures_to_repaired_with_operator_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failed_hold(7, 9)
    unpause = MagicMock()
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)
    command._repair("local", WHEN, operator="Ava #5870")
    current = pause_owner.read()
    assert current.status == "paused"
    assert current.maintenance is not None
    assert current.maintenance.failures == {}
    assert current.maintenance.repaired == {7: "RuntimeError", 9: "RuntimeError"}
    record = current.maintenance.repair_record
    assert record is not None
    assert record["by"] == "Ava #5870"
    assert record["user"]
    assert record["pid"] == str(os.getpid())
    assert record["machine"]
    assert datetime.fromisoformat(record["at"]).tzinfo is not None
    unpause.assert_called_once()
    err = capsys.readouterr().err
    assert "Repaired 2 failed receipt(s)" in err
    assert "Ava #5870" in err


def test_repair_partial_release_is_completed_by_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    failed_hold(7)

    def failing_unpause(_db: object, _bus: object) -> None:
        raise RuntimeError("posture restore failed")

    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", failing_unpause)
    with pytest.raises(RuntimeError, match="posture restore failed"):
        command._repair("local", WHEN, operator=None)
    current = pause_owner.read()
    assert current.status == "paused"
    assert current.maintenance is not None
    assert current.maintenance.failures == {}
    assert current.maintenance.repaired == {7: "RuntimeError"}
    # The repaired journal no longer blocks the ordinary cancel path.
    unpause = MagicMock()
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)
    command.resume("local", WHEN, cancel=True)
    unpause.assert_called_once()


def _refused_probe() -> None:
    raise URLError(ConnectionRefusedError(111, "Connection refused"))


def test_repair_from_drained_with_absent_agent_host_proceeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An independently absent host has no continuations; a drained hold repairs."""
    failed_hold(7, phase_value="drained")
    monkeypatch.setattr(command, "host_identity_or_none", real_host_identity_or_none)
    monkeypatch.setattr("ops.agent_pause.probe.host_identity", _refused_probe)
    monkeypatch.setattr("ops.agent_pause.probe.host_running", lambda: False)
    unpause = MagicMock()
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)

    command._repair("local", WHEN, operator=None)

    current = pause_owner.read()
    assert current.maintenance is not None
    assert current.maintenance.failures == {}
    assert current.maintenance.repaired == {7: "RuntimeError"}
    unpause.assert_called_once()
    assert "Repaired 1 failed receipt(s)" in capsys.readouterr().err


def test_repair_still_refuses_an_unreadable_agent_host_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running host with unreadable identity evidence must refuse."""
    failed_hold(7, phase_value="drained")

    def wedged() -> None:
        raise URLError(TimeoutError("timed out"))

    monkeypatch.setattr(command, "host_identity_or_none", real_host_identity_or_none)
    monkeypatch.setattr("ops.agent_pause.probe.host_identity", wedged)
    monkeypatch.setattr("ops.agent_pause.probe.host_running", lambda: True)

    with pytest.raises(URLError):
        command._repair("local", WHEN, operator=None)
    current = pause_owner.read()
    assert current.maintenance is not None
    assert current.maintenance.failures == {7: "RuntimeError"}


def test_stop_proceeds_with_absent_agent_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phase("drained")
    monkeypatch.setattr(command, "host_identity_or_none", real_host_identity_or_none)
    monkeypatch.setattr("ops.agent_pause.probe.host_identity", _refused_probe)
    monkeypatch.setattr("ops.agent_pause.probe.host_running", lambda: False)
    stop = MagicMock(return_value=[])
    monkeypatch.setattr(command, "stop_services", stop)

    command.stop("local", WHEN, 2, gateway_last=False)

    stop.assert_called_once()
    assert command._hold("local", WHEN).phase == "stopped"


@pytest.mark.parametrize("verb", ["stop", "repair"])
def test_refused_health_listener_cannot_prove_host_quiescence(
    monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    if verb == "repair":
        failed_hold(7, phase_value="drained")
    else:
        phase("drained")
    before = pause_owner.read()
    monkeypatch.setattr(command, "host_identity_or_none", real_host_identity_or_none)
    monkeypatch.setattr("ops.agent_pause.probe.host_running", lambda: True)
    monkeypatch.setattr("ops.agent_pause.probe.host_identity", _refused_probe)
    stop = MagicMock(return_value=[])
    unpause = MagicMock()
    monkeypatch.setattr(command, "stop_services", stop)
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)

    with pytest.raises(URLError):
        if verb == "stop":
            command.stop("local", WHEN, 2, gateway_last=False)
        else:
            command._repair("local", WHEN, operator=None)

    assert pause_owner.read() == before
    stop.assert_not_called()
    unpause.assert_not_called()


def test_stop_still_refuses_live_continuations(monkeypatch: pytest.MonkeyPatch) -> None:
    phase("drained")
    monkeypatch.setattr(
        command, "host_identity_or_none", lambda: HostIdentity(uuid4(), frozenset({7}))
    )
    stop = MagicMock(return_value=[])
    monkeypatch.setattr(command, "stop_services", stop)

    with pytest.raises(RuntimeError, match="still has active continuations"):
        command.stop("local", WHEN, 2, gateway_last=False)

    stop.assert_not_called()


# ── parse-layer gates (task #4092, batch B4) ──
