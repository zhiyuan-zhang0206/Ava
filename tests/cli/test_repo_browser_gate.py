"""browser is agent-runner-scoped AND gated by browser_enabled AND host capability
(browser_incapability). The start roster (_services_for_roles) drops a gated-out
browser; the diagnostic view (_services_for_roles_annotated) keeps it WITH the
reason so `ava status` / `ava start` can show why it is absent."""

import pytest

import cli.commands._repo as repo
from ops import (
    spec,  # gate reason (browser_incapability) now lives here; repo re-exports the roster
)


def _sessions(role: str) -> set[str]:
    return {s.session for s in repo._services_for_roles(frozenset({role}))}


def _annotated(role: str) -> dict[str, str | None]:
    return {
        s.session: reason for s, reason in repo._services_for_roles_annotated(frozenset({role}))
    }


def test_browser_present_on_agent_runner_when_enabled_and_capable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """browser_enabled=True AND host is capable -> browser starts."""
    monkeypatch.setattr(repo.settings.services, "browser_enabled", True)
    monkeypatch.setattr(spec, "browser_incapability", lambda: None)
    assert "browser" in _sessions("agent-runner")


def test_browser_absent_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo.settings.services, "browser_enabled", False)
    monkeypatch.setattr(spec, "browser_incapability", lambda: None)
    assert "browser" not in _sessions("agent-runner")


def test_browser_absent_when_enabled_but_not_capable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """browser_enabled=True but host not capable (no display/Chrome/npx)
    -> browser dropped from the start roster."""
    monkeypatch.setattr(repo.settings.services, "browser_enabled", True)
    monkeypatch.setattr(spec, "browser_incapability", lambda: "no display (headless)")
    assert "browser" not in _sessions("agent-runner")


def test_browser_never_on_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo.settings.services, "browser_enabled", True)
    monkeypatch.setattr(spec, "browser_incapability", lambda: None)
    assert "browser" not in _sessions("gateway")


def test_annotated_keeps_browser_with_incapability_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The diagnostic view keeps a capability-gated browser WITH its reason, even
    though the start roster drops it — so `ava status` shows why, not nothing."""
    monkeypatch.setattr(repo.settings.services, "browser_enabled", True)
    monkeypatch.setattr(spec, "browser_incapability", lambda: "no display (headless)")
    annotated = _annotated("agent-runner")
    assert annotated["browser"] == "no display (headless)"
    assert "browser" not in _sessions("agent-runner")  # start roster still drops it


def test_annotated_browser_disabled_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled (operator opt-out) is distinct from incapable, and is surfaced
    as its own reason rather than conflated with a missing display."""
    monkeypatch.setattr(repo.settings.services, "browser_enabled", False)
    monkeypatch.setattr(spec, "browser_incapability", lambda: None)
    assert _annotated("agent-runner")["browser"] == "disabled (AVA_BROWSER_ENABLED off)"


# ── computer-mcp gate (task #1101) ──────────────────────────────────────────


def test_computer_mcp_present_when_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    """No governance gate (user ruling 2026-08-10): the service runs whenever
    the platform can host it — permissions helper enabled and capable."""
    monkeypatch.setattr(repo.settings.services, "permissions_helper_enabled", True)
    monkeypatch.setattr(spec, "permissions_helper_incapability", lambda: None)
    assert "computer-mcp" in _sessions("agent-runner")


def test_computer_mcp_absent_without_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host cannot run the signed permissions helper (no swiftc/codesign/
    display on macOS, no csc on Windows) -> service gated out: the executor
    layer would have nothing to execute through."""
    monkeypatch.setattr(repo.settings.services, "permissions_helper_enabled", True)
    monkeypatch.setattr(spec, "permissions_helper_incapability", lambda: "no swiftc")
    assert "computer-mcp" not in _sessions("agent-runner")
    assert _annotated("agent-runner")["computer-mcp"] == "no swiftc"


def test_computer_mcp_absent_when_helper_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo.settings.services, "permissions_helper_enabled", False)
    monkeypatch.setattr(spec, "permissions_helper_incapability", lambda: None)
    assert "computer-mcp" not in _sessions("agent-runner")


def test_computer_mcp_never_on_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo.settings.services, "permissions_helper_enabled", True)
    monkeypatch.setattr(spec, "permissions_helper_incapability", lambda: None)
    assert "computer-mcp" not in _sessions("gateway")


def test_computer_mcp_gated_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows is the phase-3 pilot: the C# helper lacks the screen geometry /
    frontmost-app methods the executor needs, so the service stays gated out
    there until those land (task #1101)."""
    monkeypatch.setattr(repo.settings.services, "permissions_helper_enabled", True)
    monkeypatch.setattr(spec, "permissions_helper_incapability", lambda: None)
    monkeypatch.setattr(spec, "IS_WINDOWS", True)
    assert "computer-mcp" not in _sessions("agent-runner")
    assert "phase-3 pilot" in (_annotated("agent-runner")["computer-mcp"] or "")
