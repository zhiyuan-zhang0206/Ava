"""browser is agent-runner-scoped AND gated by browser_enabled AND host capability
(browser_incapability). The start roster (services_for_capabilities) drops a gated-out
browser; the diagnostic view (services_for_capabilities_annotated) keeps it WITH the
reason so `ava status` / `ava start` can show why it is absent."""

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


def test_browser_present_on_agent_runner_when_enabled_and_capable(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    """browser_enabled=True AND host is capable -> browser starts."""
    monkeypatch.setattr(settings.services, "browser_enabled", True)
    monkeypatch.setattr(spec, "browser_incapability", lambda: None)
    assert "browser" in _sessions("agent-runner", machine=command_machine)


def test_browser_absent_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    monkeypatch.setattr(settings.services, "browser_enabled", False)
    monkeypatch.setattr(spec, "browser_incapability", lambda: None)
    assert "browser" not in _sessions("agent-runner", machine=command_machine)


def test_browser_absent_when_enabled_but_not_capable(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    """browser_enabled=True but host not capable (no display/Chrome/npx)
    -> browser dropped from the start roster."""
    monkeypatch.setattr(settings.services, "browser_enabled", True)
    monkeypatch.setattr(spec, "browser_incapability", lambda: "no display (headless)")
    assert "browser" not in _sessions("agent-runner", machine=command_machine)


def test_browser_never_on_gateway(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    monkeypatch.setattr(settings.services, "browser_enabled", True)
    monkeypatch.setattr(spec, "browser_incapability", lambda: None)
    assert "browser" not in _sessions("gateway", machine=command_machine)


def test_annotated_keeps_browser_with_incapability_reason(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    """The diagnostic view keeps a capability-gated browser WITH its reason, even
    though the start roster drops it — so `ava status` shows why, not nothing."""
    monkeypatch.setattr(settings.services, "browser_enabled", True)
    monkeypatch.setattr(spec, "browser_incapability", lambda: "no display (headless)")
    annotated = _annotated("agent-runner", machine=command_machine)
    assert annotated["browser"] == "no display (headless)"
    assert "browser" not in _sessions(
        "agent-runner", machine=command_machine
    )  # start roster still drops it


def test_annotated_browser_disabled_reason(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    """Disabled (operator opt-out) is distinct from incapable, and is surfaced
    as its own reason rather than conflated with a missing display."""
    monkeypatch.setattr(settings.services, "browser_enabled", False)
    monkeypatch.setattr(spec, "browser_incapability", lambda: None)
    assert (
        _annotated("agent-runner", machine=command_machine)["browser"]
        == "disabled (AVA_BROWSER_ENABLED off)"
    )


# ── computer-mcp gate (task #1101) ──────────────────────────────────────────


def test_computer_mcp_present_when_capable(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    """No governance gate (user ruling 2026-08-10): the service runs whenever
    the platform can host it — permissions helper enabled and capable."""
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", True)
    monkeypatch.setattr(spec, "permissions_helper_incapability", lambda: None)
    assert "computer-mcp" in _sessions("agent-runner", machine=command_machine)


def test_computer_mcp_absent_without_helper(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    """The host cannot run the signed permissions helper (no swiftc/codesign/
    display on macOS) -> service gated out: the executor
    layer would have nothing to execute through."""
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", True)
    monkeypatch.setattr(spec, "permissions_helper_incapability", lambda: "no swiftc")
    assert "computer-mcp" not in _sessions("agent-runner", machine=command_machine)
    assert _annotated("agent-runner", machine=command_machine)["computer-mcp"] == "no swiftc"


def test_computer_mcp_absent_when_helper_disabled(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", False)
    monkeypatch.setattr(spec, "permissions_helper_incapability", lambda: None)
    assert "computer-mcp" not in _sessions("agent-runner", machine=command_machine)


def test_computer_mcp_never_on_gateway(
    monkeypatch: pytest.MonkeyPatch,
    command_machine: MachineIdentity,
) -> None:
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", True)
    monkeypatch.setattr(spec, "permissions_helper_incapability", lambda: None)
    assert "computer-mcp" not in _sessions("gateway", machine=command_machine)
