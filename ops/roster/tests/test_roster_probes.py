"""The roster's probe wiring: protocol availability needs no native listener proof, and the browser
verdict comes from the profile-anchored probe rather than a curl."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ops import roster


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
    monkeypatch.setattr(roster, "_browser_probe", healthy)
    monkeypatch.setattr("ops.roster.healthz.probe_daemon", healthy)

    def identity(*_args: object) -> Callable[[], DaemonProbe]:
        return healthy

    monkeypatch.setattr(roster, "daemon_identity", identity)
    monkeypatch.setattr("psutil.process_iter", forbidden)
    monkeypatch.setattr("base.native_process.root_control.client.owned_process", forbidden)
    item = next(item for item in roster.build_services() if item.session == service)
    assert item.identity_probe is not None
    assert item.identity_probe().alive


def test_browser_identity_is_the_profile_probe_not_a_curl() -> None:
    """The browser's `curl_url` stays (the CDP endpoint is still what gets
    dialled), but the verdict comes from the profile-anchored probe — CDP itself
    carries no field we control, so a 200 there says nothing about whose Chrome
    answered."""
    from services.desktop.browser.probe import probe_browser

    browser = next(s for s in roster.build_services() if s.session == "browser")
    probe = browser.identity_probe
    assert probe is roster._browser_probe
    assert probe_browser is not None  # the lazy import target exists
