"""Root observations must distinguish evidence, freshness, and recovery authority."""

from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.ava_root.health import ProbeRunner
from services.ava_root_glue import diagnostic_probes as probes
from services.ava_root_glue.diagnostics import Diagnostic, DiagnosticMonitor, RootHealthRounds
from shared.daemon_health import DaemonProbe


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    monkeypatch.setattr("shared.log.init_gateway_process", lambda **_: None)
    monkeypatch.setattr("shared.telemetry.sync", lambda **_: None)
    monkeypatch.setattr(
        "shared.log.logger",
        SimpleNamespace(
            info=lambda *_, **fields: records.append(fields),
            warning=lambda *_, **fields: records.append(fields),
            log=lambda *_, **fields: records.append(fields),
        ),
    )
    return records


async def test_deadline_retains_one_worker_and_discards_late_green() -> None:
    entered, release, exited = Event(), Event(), Event()
    calls = 0

    def stuck() -> DaemonProbe:
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(2)
        exited.set()
        return DaemonProbe.up("late green")

    runner = ProbeRunner()
    try:
        result = await runner.observe(stuck, 0.01)
        assert entered.is_set()
        assert result.verdict.value == "unavailable"
        result = await runner.observe(stuck, 0.01)
        assert result.verdict.value == "unavailable"
        assert calls == 1
    finally:
        release.set()
    while not exited.is_set():
        await asyncio.sleep(0.001)
    result = await runner.observe(lambda: DaemonProbe.down("fresh failure"), 0.1)
    assert result.verdict.value == "down"
    assert result.detail == "fresh failure"


async def test_probe_bug_is_unknown_evidence() -> None:
    def broken() -> DaemonProbe:
        raise OSError("inspection denied")

    result = await ProbeRunner().observe(broken, 0.1)
    assert result.verdict.value == "unavailable"


async def test_slow_diagnostic_cannot_block_peer_or_duplicate_itself(events) -> None:
    release = Event()
    calls = 0

    def blocked() -> DaemonProbe:
        nonlocal calls
        calls += 1
        release.wait(2)
        return DaemonProbe.up("late")

    monitor = DiagnosticMonitor(
        [
            Diagnostic("blocked", blocked, interval_s=0.001, timeout_s=0.01),
            Diagnostic("peer", lambda: DaemonProbe.down("observed failure"), interval_s=0.001),
        ]
    )
    try:
        before = monitor.health_snapshot()
        assert before["diagnostic:blocked"]["sampled_at"] is None
        assert before["diagnostic:blocked"]["last_verdict"] is None
        await monitor.run_round()
        await asyncio.sleep(0.002)
        await monitor.run_round()
        snapshot = monitor.health_snapshot()
        assert snapshot["diagnostic:blocked"]["last_verdict"] == "unavailable"
        assert snapshot["diagnostic:peer"]["last_verdict"] == "down"
        assert calls == 1
    finally:
        release.set()


async def test_unknown_never_resolves_station_alert_or_counts_failed_canaries(events) -> None:
    current = DaemonProbe.down("canary failed")
    reported: list[DaemonProbe] = []
    monitor = DiagnosticMonitor(
        [
            Diagnostic(
                "canary",
                lambda: current,
                interval_s=0.001,
                failure_threshold=2,
                report=reported.append,
            )
        ]
    )
    await monitor.run_round()
    assert not events
    current = DaemonProbe.unavailable("no host baseline")
    await asyncio.sleep(0.002)
    await monitor.run_round()
    assert len(reported) == 1  # unknown did not fire or recover the external alert
    state = monitor.health_snapshot()["diagnostic:canary"]
    assert state["consecutive_failures"] == 0
    assert state["last_verdict"] == "unavailable"


class _Health:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.fail = False

    async def run_round(self) -> None:
        await self.release.wait()
        if self.fail:
            raise RuntimeError("round coordinator failed")

    def health_snapshot(self) -> dict[str, object]:
        return {"service": {"last_verdict": "down"}}


async def test_tick_requires_both_rounds_and_does_not_mean_healthy(events) -> None:
    health = _Health()
    diagnostics = DiagnosticMonitor([Diagnostic("failed", lambda: DaemonProbe.down("failed"))])
    rounds = RootHealthRounds(health, diagnostics)
    task = asyncio.create_task(rounds.run_round())
    await asyncio.sleep(0.01)
    assert not any(event["event"] == "root_health_tick" for event in events)
    health.release.set()
    await task
    assert any(event["event"] == "root_health_tick" for event in events)
    assert rounds.health_snapshot()["diagnostic:failed"]["last_verdict"] == "down"
    events.clear()
    health.fail = True
    with pytest.raises(RuntimeError):
        await rounds.run_round()
    assert not any(event["event"] == "root_health_tick" for event in events)


async def test_expectation_precedes_first_sample_and_is_retired_on_stop(events) -> None:
    health = _Health()
    rounds = RootHealthRounds(health, DiagnosticMonitor([]))
    await rounds.start()
    assert events[0]["event"] == "root_health_expected"
    assert events[0]["expected_since_timestamp_seconds"] > 0
    assert len(events[0]["home_id"]) == 64
    assert rounds.health_snapshot()["observer:root-health"]["last_completed_at"] is None
    await rounds.stop()
    assert events[-1]["event"] == "root_health_expected"
    assert events[-1]["expected_since_timestamp_seconds"] == 0
    assert events[-1]["home_id"] == events[0]["home_id"]
    assert not any(event["event"] == "root_health_tick" for event in events)


def test_helper_diagnostics_are_macos_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probes, "IS_MACOS", False)
    monkeypatch.setattr(probes, "IS_WINDOWS", False)
    monkeypatch.setattr("shared.machine.is_gateway", lambda: False)
    names = {check.name for check in probes.build_diagnostics(set())}
    assert names == {"venv"}
    monkeypatch.setattr(probes, "IS_MACOS", True)
    monkeypatch.setattr(
        probes,
        "settings",
        SimpleNamespace(services=SimpleNamespace(permissions_helper_enabled=False)),
    )
    names = {check.name for check in probes.build_diagnostics(set())}
    assert names == {"venv", "brew-pin", "permissions-helper"}


def test_station_no_credential_is_unknown_and_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = probes.StationProbe()
    answers = Mock(return_value=True)
    monkeypatch.setattr(probe._module, "_station_answers", answers)
    monkeypatch.setattr(
        probes, "settings", SimpleNamespace(data_plane=SimpleNamespace(cluster_secret=""))
    )
    assert probe.probe().verdict.value == "unavailable"
    answers.assert_not_called()


def test_browser_canary_runs_only_inside_owned_endpoint_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.healthchecks import browser_reach, owned_service

    canary = Mock(side_effect=AssertionError("foreign browser must not be used"))
    monkeypatch.setattr(browser_reach, "_canary", canary)
    monkeypatch.setattr(
        owned_service, "probe_endpoint", lambda *_: DaemonProbe.port_taken("foreign")
    )
    monkeypatch.setattr(
        probes,
        "settings",
        SimpleNamespace(
            services=SimpleNamespace(
                gateway_health_url="https://example.invalid/api/health",
                browser_cdp_port=9222,
            )
        ),
    )
    assert probes.browser_reach().verdict.value == "port-taken"
    canary.assert_not_called()


def test_pooler_requires_native_custody_before_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import _maintenance_data_plane as native
    from cli.commands import _pgbouncer

    listener = Mock(side_effect=AssertionError("unknown pooler must not be accepted"))
    monkeypatch.setattr("shared.cluster.get_record", lambda _: object())
    monkeypatch.setattr(native, "_capture_pooler", lambda: None)
    monkeypatch.setattr(_pgbouncer, "pgbouncer_listener_reachable", listener)
    assert probes.pgbouncer().verdict.value == "down"
    listener.assert_not_called()


def test_missing_brew_probe_is_unavailable_not_an_empty_healthy_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*_, **__):
        raise FileNotFoundError("brew")

    monkeypatch.setattr("shared.proc.run_bounded", missing)
    with pytest.raises(FileNotFoundError):
        probes.brew_pins()  # ProbeRunner maps inspection failure to UNAVAILABLE.
