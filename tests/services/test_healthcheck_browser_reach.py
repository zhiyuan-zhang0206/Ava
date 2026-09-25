"""Browser-vs-host diagnostic evidence, under the root's ownership envelope."""

from types import SimpleNamespace

import pytest

from services.ava_root_glue import diagnostic_probes as probes
from services.healthchecks import browser_reach as hc
from shared.daemon_health import DaemonProbe


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
def test_canary_distinguishes_browser_failure_from_unknown(monkeypatch, outcome, host_ok, expected):
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
    monkeypatch.setattr(
        "services.healthchecks.owned_service.probe_endpoint", lambda _name, _port, call: call()
    )
    monkeypatch.setattr("services.browser.probe.probe_browser", lambda: DaemonProbe.up("owned"))
    monkeypatch.setattr(hc, "_canary", lambda *_: hc._CanaryResult(outcome, "browser evidence"))
    monkeypatch.setattr(hc, "_host_probe", lambda *_: hc._HostResult(host_ok, "host evidence"))
    assert probes.browser_reach().verdict.value == expected


def test_canary_unexpected_failure_is_not_reachability_success(monkeypatch):
    async def fail(*_):
        raise RuntimeError("CDP failed")

    monkeypatch.setattr(hc, "_canary_async", fail)
    assert hc._canary(9222, "https://example.invalid/", 1).outcome == "skip"
