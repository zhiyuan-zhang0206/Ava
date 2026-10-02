"""Browser-vs-host diagnostic evidence, under the root's ownership envelope."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from base.daemon.health import DaemonProbe
from services.ava_root_glue import diagnostic_probes as probes
from services.healthchecks import browser_reach as hc


@pytest.mark.parametrize(
    "outcome,host_ok,expected",
    [
        ("ok", True, "alive"),
        ("timeout", True, "down"),
        ("error", True, "down"),
        ("timeout", False, "unavailable"),
        ("skip", True, "unavailable"),
    ],
)
def test_canary_distinguishes_browser_failure_from_unknown(
    monkeypatch: pytest.MonkeyPatch, outcome: str, host_ok: bool, expected: str
) -> None:
    def _fake_probe_endpoint(
        _name: str, _port: int, call: Callable[[], bool | DaemonProbe]
    ) -> bool | DaemonProbe:
        return call()

    def _fake_probe_browser(_port: int | None = None, _profile: Path | None = None) -> DaemonProbe:
        return DaemonProbe.up("owned")

    def _fake_canary(*_a: object) -> hc._CanaryResult:
        return hc._CanaryResult(outcome, "browser evidence")

    def _fake_host_probe(*_a: object) -> hc._HostResult:
        return hc._HostResult(host_ok, "host evidence")

    monkeypatch.setattr(
        probes,
        "settings",
        SimpleNamespace(
            services=SimpleNamespace(
                gateway_health_url="https://example.invalid/health",
                browser_cdp_port=9222,
                browser_reach_timeout_s=1,
            )
        ),
    )
    monkeypatch.setattr("services.healthchecks.owned_service.probe_endpoint", _fake_probe_endpoint)
    monkeypatch.setattr("services.browser.probe.probe_browser", _fake_probe_browser)
    monkeypatch.setattr(hc, "canary", _fake_canary)
    monkeypatch.setattr(hc, "host_probe", _fake_host_probe)
    assert probes.browser_reach().verdict.value == expected
