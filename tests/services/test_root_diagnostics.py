"""Root observations must distinguish evidence, freshness, and recovery authority."""

from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace
from typing import NoReturn, cast
from unittest.mock import Mock

import pytest

from services.ava_root.health import HealthMonitor, ProbeRunner
from services.ava_root.probes import ProbeRegistry
from services.ava_root_glue import diagnostic_probes as probes
from services.ava_root_glue.diagnostics import Diagnostic, DiagnosticMonitor, RootHealthRounds
from shared.daemon_health import DaemonProbe
from shared.native_process.ownership import OwnedProcess


class _EventLog:
    """Structured log calls captured as their field dictionaries."""

    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def info(self, *_args: object, **fields: object) -> None:
        self._records.append(fields)

    warning = info
    log = info


def _no_op(**_kwargs: object) -> None:
    return None


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    monkeypatch.setattr("shared.log.init_gateway_process", _no_op)
    monkeypatch.setattr("shared.telemetry.sync", _no_op)
    monkeypatch.setattr("shared.log.logger", _EventLog(records))
    return records


def _entry(snapshot: dict[str, object], key: str) -> dict[str, object]:
    """One health-surface entry; every entry is a field mapping."""
    value = snapshot[key]
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


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


async def test_slow_diagnostic_cannot_block_peer_or_duplicate_itself(
    events: list[dict[str, object]],
) -> None:
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
        before = _entry(monitor.health_snapshot(), "diagnostic:blocked")
        assert before["sampled_at"] is None
        assert before["last_verdict"] is None
        await monitor.run_round()
        await asyncio.sleep(0.002)
        await monitor.run_round()
        snapshot = monitor.health_snapshot()
        assert _entry(snapshot, "diagnostic:blocked")["last_verdict"] == "unavailable"
        assert _entry(snapshot, "diagnostic:peer")["last_verdict"] == "down"
        assert calls == 1
    finally:
        release.set()


async def test_unknown_never_resolves_station_alert_or_counts_failed_canaries(
    events: list[dict[str, object]],
) -> None:
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
    state = _entry(monitor.health_snapshot(), "diagnostic:canary")
    assert state["consecutive_failures"] == 0
    assert state["last_verdict"] == "unavailable"


class _NoRevival:
    """The supervisor slice a round-level fake never reaches."""

    async def restart(self, unit_id: str) -> dict[str, object]:
        raise AssertionError(f"unexpected restart of {unit_id}")

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float] | None:
        raise AssertionError(f"unexpected generation lookup for {unit_id}")

    def revival_deferral(self, unit_id: str) -> str | None:
        raise AssertionError(f"unexpected revival check for {unit_id}")


class _Health(HealthMonitor):
    """A service health round the test releases (or fails) explicitly."""

    def __init__(self) -> None:
        super().__init__(_NoRevival(), ProbeRegistry())
        self.release = asyncio.Event()
        self.fail = False

    async def run_round(self) -> None:
        await self.release.wait()
        if self.fail:
            raise RuntimeError("round coordinator failed")

    def health_snapshot(self) -> dict[str, object]:
        return {"service": {"last_verdict": "down"}}


async def test_tick_requires_both_rounds_and_does_not_mean_healthy(
    events: list[dict[str, object]],
) -> None:
    health = _Health()
    diagnostics = DiagnosticMonitor([Diagnostic("failed", lambda: DaemonProbe.down("failed"))])
    rounds = RootHealthRounds(health, diagnostics)
    task = asyncio.create_task(rounds.run_round())
    await asyncio.sleep(0.01)
    assert not any(event["event"] == "root_health_tick" for event in events)
    health.release.set()
    await task
    assert any(event["event"] == "root_health_tick" for event in events)
    assert _entry(rounds.health_snapshot(), "diagnostic:failed")["last_verdict"] == "down"
    events.clear()
    health.fail = True
    with pytest.raises(RuntimeError):
        await rounds.run_round()
    assert not any(event["event"] == "root_health_tick" for event in events)


async def test_expectation_precedes_first_sample_and_is_retired_on_stop(
    events: list[dict[str, object]],
) -> None:
    health = _Health()
    rounds = RootHealthRounds(health, DiagnosticMonitor([]))
    await rounds.start()
    assert events[0]["event"] == "root_health_expected"
    since = events[0]["expected_since_timestamp_seconds"]
    assert isinstance(since, float) and since > 0
    home_id = events[0]["home_id"]
    assert isinstance(home_id, str) and len(home_id) == 64
    assert _entry(rounds.health_snapshot(), "observer:root-health")["last_completed_at"] is None
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

    def foreign_listener(*_args: object) -> DaemonProbe:
        return DaemonProbe.port_taken("foreign")

    canary = Mock(side_effect=AssertionError("foreign browser must not be used"))
    monkeypatch.setattr(browser_reach, "_canary", canary)
    monkeypatch.setattr(owned_service, "probe_endpoint", foreign_listener)
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
    from cli.commands import _pgbouncer
    from shared.cluster import ownership

    def registered(_home: object) -> object:
        return object()

    listener = Mock(side_effect=AssertionError("unknown pooler must not be accepted"))
    monkeypatch.setattr("shared.cluster.get_record", registered)
    monkeypatch.setattr(ownership, "pooler", Mock(return_value=None))
    monkeypatch.setattr(_pgbouncer, "pgbouncer_listener_reachable", listener)
    assert probes.pgbouncer().verdict.value == "down"
    listener.assert_not_called()


def test_missing_brew_probe_is_unavailable_not_an_empty_healthy_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*_args: object, **_kwargs: object) -> NoReturn:
        raise FileNotFoundError("brew")

    monkeypatch.setattr("shared.proc.run_bounded", missing)
    with pytest.raises(FileNotFoundError):
        probes.brew_pins()  # ProbeRunner maps inspection failure to UNAVAILABLE.
