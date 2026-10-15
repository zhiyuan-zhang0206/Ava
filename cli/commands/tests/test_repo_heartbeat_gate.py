import pytest

from base.cluster.machine import MachineIdentity
from base.config import settings
from base.host.system.tests.fake_commands import fake_host
from cli.database.tests.command_inputs import command_machine as command_machine
from services.supervision.roster import spec


def _sessions(role: str, *, machine: MachineIdentity) -> set[str]:
    return {
        s.session
        for s in spec.services_for_capabilities(
            frozenset({role}), machine=machine, host=fake_host()
        )
    }


def _annotated(role: str, *, machine: MachineIdentity) -> dict[str, str | None]:
    return {
        s.session: reason
        for s, reason in spec.services_for_capabilities_annotated(
            frozenset({role}), machine=machine, host=fake_host()
        )
    }


def test_heartbeat_present_on_gateway_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", True)
    assert "heartbeat" in _sessions("gateway", machine=command_machine)


def test_heartbeat_absent_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", False)
    assert "heartbeat" not in _sessions("gateway", machine=command_machine)


def test_heartbeat_never_on_agent_runner(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", True)
    assert "heartbeat" not in _sessions("agent-runner", machine=command_machine)


def test_annotated_keeps_heartbeat_with_disabled_reason(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", False)
    annotated = _annotated("gateway", machine=command_machine)
    assert annotated["heartbeat"] == "disabled (AVA_HEARTBEAT_ENABLED off)"
    assert "heartbeat" not in _sessions("gateway", machine=command_machine)


def test_annotated_heartbeat_enabled_has_no_reason(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", True)
    assert _annotated("gateway", machine=command_machine)["heartbeat"] is None
