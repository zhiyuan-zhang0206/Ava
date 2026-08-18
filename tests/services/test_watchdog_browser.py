"""Watchdog includes the ava-browser healthcheck on an agent-runner only when the
browser is enabled AND the host is capable (display + Chrome + npx) — so Chrome
death is detected and respawned only on capable machines. When enabled but
incapable, the round logs WHY (browser_incapability reason) instead of going
silent."""

import logging

import pytest

import services.watchdog.daemon as wd
from shared.machine import MachineRole


def _names(
    monkeypatch: pytest.MonkeyPatch, role: MachineRole, *, enabled: bool, capable: bool = True
) -> list[str]:
    # Gating now flows through build_services()/_gate_reason (the single source the
    # watchdog roster is derived from), so patch the inputs _gate_reason reads:
    # settings.services.browser_enabled and cli.commands._repo.browser_incapability.
    monkeypatch.setattr(wd.settings.services, "browser_enabled", enabled)
    monkeypatch.setattr(
        "ops.spec.browser_incapability",
        lambda: None if capable else "no display (headless)",
    )
    return [c.name for c in wd._checks_for_capability(role)]


def test_agent_runner_includes_browser_when_enabled_and_capable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "browser" in _names(monkeypatch, "agent-runner", enabled=True, capable=True)


def test_agent_runner_excludes_browser_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "browser" not in _names(monkeypatch, "agent-runner", enabled=False, capable=True)


def test_agent_runner_excludes_browser_when_not_capable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with browser_enabled=True, an incapable host skips the healthcheck."""
    assert "browser" not in _names(monkeypatch, "agent-runner", enabled=True, capable=False)


def test_gateway_never_includes_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "browser" not in _names(monkeypatch, "gateway", enabled=True, capable=True)


def test_incapable_browser_logs_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An enabled-but-incapable agent-runner logs WHY browser is skipped (debug,
    since the round fires every 60s) — the bug was zero log signal. The reason now
    comes from _gate_reason (the same one `ava status` shows)."""
    monkeypatch.setattr(wd.settings.services, "browser_enabled", True)
    monkeypatch.setattr("ops.spec.browser_incapability", lambda: "no display (headless)")
    with caplog.at_level(logging.DEBUG, logger="services.watchdog.daemon"):
        wd._checks_for_capability("agent-runner")
    assert any(
        "browser not revived" in r.getMessage() and "no display" in r.getMessage()
        for r in caplog.records
    )
