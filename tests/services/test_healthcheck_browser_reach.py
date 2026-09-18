"""browser-reach healthcheck — the shared browser's network face vs the host path.

Locks the behavior the 2026-09-18 pool-hang incident (task #3921) asked for:
the canary contrasts a browser-path fetch with a same-process host read, counts
consecutive failing probes, reports ONE ERROR with both raw readings + the
recipe pointer once the threshold is crossed, stays quiet while the episode
persists, and re-arms after a healthy round. A host-side outage (both paths
failing) is not this check's condition and must not accumulate.
"""

from __future__ import annotations

import logging

import pytest

from services.healthchecks import browser_reach as hc
from shared.daemon_health import DaemonProbe


def _noop(*_args: object, **_kwargs: object) -> None:
    pass


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "init_gateway_process", _noop)
    monkeypatch.setattr(hc, "_last_probe_monotonic", None)
    monkeypatch.setattr(hc, "_consecutive_failures", 0)
    monkeypatch.setattr(hc, "_reported", False)
    monkeypatch.setattr(hc, "probe_browser", lambda: DaemonProbe.up("pid 123"))
    monkeypatch.setattr(hc.settings.services, "browser_reach_probe_interval_s", 0)
    monkeypatch.setattr(hc.settings.services, "browser_reach_timeout_s", 1.0)
    monkeypatch.setattr(hc.settings.services, "browser_reach_failure_threshold", 3)
    monkeypatch.setattr(hc.settings.services, "gateway_health_url", "http://gw.example/api/health")


def _set_canary(
    monkeypatch: pytest.MonkeyPatch, outcome: str, calls: list[str] | None = None
) -> None:
    def fake_canary(port: int, url: str, timeout_s: float) -> hc._CanaryResult:
        if calls is not None:
            calls.append(outcome)
        return hc._CanaryResult(outcome, f"canary-detail:{outcome}")

    monkeypatch.setattr(hc, "_canary", fake_canary)


def _set_host(monkeypatch: pytest.MonkeyPatch, ok: bool) -> None:
    def fake_host(url: str, timeout_s: float) -> hc._HostResult:
        return hc._HostResult(ok=ok, detail="host-detail:ok" if ok else "host-detail:down")

    monkeypatch.setattr(hc, "_host_probe", fake_host)


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == hc._log.name and record.levelno == level
    ]


def test_reports_once_after_threshold_with_both_readings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=hc._log.name)
    _set_canary(monkeypatch, "timeout")
    _set_host(monkeypatch, ok=True)

    hc.main()
    hc.main()
    assert _records(caplog, logging.ERROR) == []
    assert hc._consecutive_failures == 2

    hc.main()
    errors = _records(caplog, logging.ERROR)
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "browser path=canary-detail:timeout" in message
    assert "host path=host-detail:ok" in message
    assert hc._RECIPE_POINTER in message

    hc.main()  # still failing, already reported: quiet
    assert len(_records(caplog, logging.ERROR)) == 1


def test_error_outcome_counts_like_a_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=hc._log.name)
    _set_canary(monkeypatch, "error")
    _set_host(monkeypatch, ok=True)
    for _ in range(3):
        hc.main()
    assert len(_records(caplog, logging.ERROR)) == 1


def test_healthy_round_recovers_and_rearms(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=hc._log.name)
    _set_canary(monkeypatch, "timeout")
    _set_host(monkeypatch, ok=True)
    for _ in range(3):
        hc.main()
    assert len(_records(caplog, logging.ERROR)) == 1

    _set_canary(monkeypatch, "ok")
    hc.main()
    infos = _records(caplog, logging.INFO)
    assert len(infos) == 1
    assert "recovered" in infos[0].getMessage()
    assert hc._reported is False

    _set_canary(monkeypatch, "timeout")
    hc.main()
    hc.main()
    assert len(_records(caplog, logging.ERROR)) == 1  # re-armed: 2 failures do not report
    hc.main()
    assert len(_records(caplog, logging.ERROR)) == 2


def test_both_paths_failing_is_not_our_condition(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=hc._log.name)
    _set_canary(monkeypatch, "timeout")
    _set_host(monkeypatch, ok=False)
    for _ in range(5):
        hc.main()
    assert _records(caplog, logging.ERROR) == []
    assert hc._consecutive_failures == 0

    _set_host(monkeypatch, ok=True)
    hc.main()
    hc.main()
    assert _records(caplog, logging.ERROR) == []
    hc.main()
    assert len(_records(caplog, logging.ERROR)) == 1


def test_skips_and_resets_when_browser_not_probe_alive(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=hc._log.name)
    calls: list[str] = []
    _set_canary(monkeypatch, "timeout", calls)
    monkeypatch.setattr(hc, "_consecutive_failures", 2)
    monkeypatch.setattr(hc, "probe_browser", lambda: DaemonProbe.down("CDP refused"))

    hc.main()
    assert calls == []
    assert _records(caplog, logging.ERROR) == []
    assert hc._consecutive_failures == 0


def test_throttle_skips_rounds_within_interval(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=hc._log.name)
    calls: list[str] = []
    _set_canary(monkeypatch, "ok", calls)
    _set_host(monkeypatch, ok=True)
    monkeypatch.setattr(hc.settings.services, "browser_reach_probe_interval_s", 60)

    hc.main()
    hc.main()
    assert calls == ["ok"]

    monkeypatch.setattr(hc, "_last_probe_monotonic", hc.time.monotonic() - 61)
    hc.main()
    assert calls == ["ok", "ok"]


def test_canary_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=hc._log.name)
    _set_host(monkeypatch, ok=True)

    def boom(port: int, url: str, timeout_s: float) -> hc._CanaryResult:
        raise RuntimeError("cdp exploded")

    monkeypatch.setattr(hc, "_canary_async", boom)
    hc.main()  # a raising canary is a skip, not a verdict — must not propagate
    assert _records(caplog, logging.ERROR) == []
