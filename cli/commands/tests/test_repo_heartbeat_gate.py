"""heartbeat is gateway-scoped AND gated by heartbeat_enabled.
The start roster (services_for_capabilities) drops a gated-out heartbeat; the diagnostic
view (services_for_capabilities_annotated) keeps it WITH the reason so `ava status` /
`ava start` can show why it is absent."""

import pytest

from base.cluster.machine import MachineIdentity
from base.config import settings
from cli.tests.bootstrap.command_inputs import command_machine as command_machine
from services.supervision.roster import spec


def _sessions(role: str, *, machine: MachineIdentity) -> set[str]:
    return {s.session for s in spec.services_for_capabilities(frozenset({role}), machine=machine)}


def _annotated(role: str, *, machine: MachineIdentity) -> dict[str, str | None]:
    return {
        s.session: reason
        for s, reason in spec.services_for_capabilities_annotated(
            frozenset({role}), machine=machine
        )
    }


def test_heartbeat_present_on_gateway_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    """heartbeat_enabled=True (default) -> heartbeat starts on gateway."""
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", True)
    assert "heartbeat" in _sessions("gateway", machine=command_machine)


def test_heartbeat_absent_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    """heartbeat_enabled=False -> heartbeat dropped from the start roster."""
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", False)
    assert "heartbeat" not in _sessions("gateway", machine=command_machine)


def test_heartbeat_never_on_agent_runner(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    """Heartbeat is gateway-scoped — never on agent-runner, even when enabled."""
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", True)
    assert "heartbeat" not in _sessions("agent-runner", machine=command_machine)


def test_annotated_keeps_heartbeat_with_disabled_reason(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    """The diagnostic view keeps a disabled heartbeat WITH its reason, even
    though the start roster drops it — so `ava status` shows why."""
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", False)
    annotated = _annotated("gateway", machine=command_machine)
    assert annotated["heartbeat"] == "disabled (AVA_HEARTBEAT_ENABLED off)"
    assert "heartbeat" not in _sessions(
        "gateway", machine=command_machine
    )  # start roster still drops it


def test_annotated_heartbeat_enabled_has_no_reason(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    """When enabled (default), the gate reason is None — no gate blocks it."""
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", True)
    assert _annotated("gateway", machine=command_machine)["heartbeat"] is None
