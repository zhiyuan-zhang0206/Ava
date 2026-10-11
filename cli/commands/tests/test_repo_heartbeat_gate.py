"""heartbeat is gateway-scoped AND gated by heartbeat_enabled.
The start roster (services_for_capabilities) drops a gated-out heartbeat; the diagnostic
view (services_for_capabilities_annotated) keeps it WITH the reason so `ava status` /
`ava start` can show why it is absent."""

import pytest

from base.config import settings
from ops import spec


def _sessions(role: str) -> set[str]:
    return {s.session for s in spec.services_for_capabilities(frozenset({role}))}


def _annotated(role: str) -> dict[str, str | None]:
    return {
        s.session: reason
        for s, reason in spec.services_for_capabilities_annotated(frozenset({role}))
    }


def test_heartbeat_present_on_gateway_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """heartbeat_enabled=True (default) -> heartbeat starts on gateway."""
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", True)
    assert "heartbeat" in _sessions("gateway")


def test_heartbeat_absent_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """heartbeat_enabled=False -> heartbeat dropped from the start roster."""
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", False)
    assert "heartbeat" not in _sessions("gateway")


def test_heartbeat_never_on_agent_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heartbeat is gateway-scoped — never on agent-runner, even when enabled."""
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", True)
    assert "heartbeat" not in _sessions("agent-runner")


def test_annotated_keeps_heartbeat_with_disabled_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The diagnostic view keeps a disabled heartbeat WITH its reason, even
    though the start roster drops it — so `ava status` shows why."""
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", False)
    annotated = _annotated("gateway")
    assert annotated["heartbeat"] == "disabled (AVA_HEARTBEAT_ENABLED off)"
    assert "heartbeat" not in _sessions("gateway")  # start roster still drops it


def test_annotated_heartbeat_enabled_has_no_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When enabled (default), the gate reason is None — no gate blocks it."""
    monkeypatch.setattr(settings.daemon, "heartbeat_enabled", True)
    assert _annotated("gateway")["heartbeat"] is None
