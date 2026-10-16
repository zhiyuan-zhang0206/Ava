from __future__ import annotations

from collections.abc import Callable

import pytest

from base.native_process.root_control import client
from services.supervision import roster
from services.supervision.roster import healthz


@pytest.mark.parametrize("service", ["gateway", "browser", "task-maintenance"])
def test_roster_uses_healthy_protocol_without_native_listener_proof(
    monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    from base.daemon.health import DaemonProbe

    def healthy(*_args: object, **_kwargs: object) -> DaemonProbe:
        return DaemonProbe.up("healthy")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("roster protocol availability must not inspect native ownership")

    monkeypatch.setattr(roster, "probe_home", healthy)
    monkeypatch.setattr(roster, "browser_probe", healthy)
    monkeypatch.setattr(healthz, "probe_daemon", healthy)

    def identity(*_args: object) -> Callable[[], DaemonProbe]:
        return healthy

    monkeypatch.setattr(roster, "daemon_identity", identity)
    monkeypatch.setattr("psutil.process_iter", forbidden)
    monkeypatch.setattr(client, "root_process", forbidden)
    item = next(item for item in roster.build_services() if item.session == service)
    assert item.identity_probe is not None
    assert item.identity_probe().alive


def test_browser_identity_is_the_profile_probe_not_a_curl() -> None:
    from services.desktop.browser.probe import probe_browser

    browser = next(s for s in roster.build_services() if s.session == "browser")
    probe = browser.identity_probe
    assert probe is roster.browser_probe
    assert probe_browser is not None
