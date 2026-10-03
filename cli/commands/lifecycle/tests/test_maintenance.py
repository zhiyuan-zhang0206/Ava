"""`maintenance repair` and `cancel` keep the hold unless the release fully lands."""

import os
from datetime import datetime
from unittest.mock import MagicMock
from urllib.error import URLError
from uuid import uuid4

import pytest

from base.db.tests.fakes import patch_database
from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenanceHold
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
    monkeypatch.setattr("ops.agent_pause.publish_inbound_wake", MagicMock())
    monkeypatch.setattr("base.deploy.state.host_deploy_state.set_posture", MagicMock())


def phase(value: str) -> None:
    before = pause_owner.begin_maintenance("local", WHEN).snapshot
    assert before.maintenance is not None
    hold = MaintenanceHold.decode({**before.maintenance.encode(), "phase": value})
    pause_owner.change_maintenance("local", WHEN, before.maintenance, hold)


def test_failed_dependency_cancel_never_releases_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    phase("preparing")

    def unavailable() -> None:
        raise ConnectionError("dependencies unavailable")

    patch_database(monkeypatch, connect=unavailable)
    with pytest.raises(ConnectionError):
        command.cancel("local", WHEN)
    assert command._hold("local", WHEN).phase == "preparing"


def test_cancel_proceeds_with_absent_agent_host(
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

    command.cancel("local", WHEN)

    unpause.assert_called_once()
    assert "provably absent" in capsys.readouterr().err


def test_cancel_still_refuses_an_unreadable_host_probe(
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
        command.cancel("local", WHEN)
    assert pause_owner.read().status == "paused"


@pytest.mark.parametrize("value", ["stopping", "stopped", "starting", "ready"])
def test_cancel_cannot_bypass_stop_or_start_readiness(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    phase(value)
    unpause = MagicMock()
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)
    with pytest.raises(RuntimeError, match="cancel cannot bypass"):
        command.cancel("local", WHEN)
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
    command.cancel("local", WHEN)
    unpause.assert_called_once()
    assert not admission.held()


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
    command.cancel("local", WHEN)
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


def test_refused_health_listener_cannot_prove_host_quiescence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_hold(7, phase_value="drained")
    before = pause_owner.read()
    monkeypatch.setattr(command, "host_identity_or_none", real_host_identity_or_none)
    monkeypatch.setattr("ops.agent_pause.probe.host_running", lambda: True)
    monkeypatch.setattr("ops.agent_pause.probe.host_identity", _refused_probe)
    unpause = MagicMock()
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", unpause)

    with pytest.raises(URLError):
        command._repair("local", WHEN, operator=None)

    assert pause_owner.read() == before
    unpause.assert_not_called()
