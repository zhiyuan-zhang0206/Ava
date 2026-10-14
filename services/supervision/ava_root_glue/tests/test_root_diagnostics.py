"""Root observations must distinguish evidence, freshness, and recovery authority."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import NoReturn, cast
from unittest.mock import Mock

import pytest

from base import telemetry
from base.agents.context.clients import ClientSet
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.daemon.health import DaemonProbe
from base.host import proc
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.loaded_commit import LoadedCommit
from base.native_process.ownership import OwnedProcess
from base.telemetry import process_logging
from base.telemetry.delivery.pipeline import EventPipeline
from services.supervision.ava_root.failure_state import UnitFailureFacts
from services.supervision.ava_root.health import HealthMonitor, ProbeRunner
from services.supervision.ava_root.probes import ProbeRegistry
from services.supervision.ava_root_glue import diagnostic_probes as probes
from services.supervision.ava_root_glue.diagnostics import (
    Diagnostic,
    DiagnosticMonitor,
    RootHealthRounds,
)


class _Events:
    """The structured log calls so far, as their field dictionaries (read from `loguru_records`).

    Only records logged with an `event=` field count: the diagnostics' own signals, not the
    incidental lines other code logs while a round runs."""

    def __init__(self, records: LogRecords) -> None:
        self._records = records

    def _fields(self) -> list[dict[str, object]]:
        return [dict(r["extra"]) for r in self._records if "event" in r["extra"]]

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._fields())

    def __getitem__(self, index: int) -> dict[str, object]:
        return self._fields()[index]

    def clear(self) -> None:
        self._records.clear()


def _unused_database() -> NoReturn:
    raise AssertionError("this diagnostic must not construct a database")


def _no_op(**_kwargs: object) -> None:
    return None


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords) -> _Events:
    monkeypatch.setattr(process_logging, "init_gateway_process", _no_op)
    monkeypatch.setattr(telemetry, "sync", _no_op)
    return _Events(loguru_records)


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
    events: _Events,
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


async def test_reporting_never_races_a_settling_worker(caplog: pytest.LogCaptureFixture) -> None:
    """The 10-14 echo: a worker whose result is complete but whose thread has not
    retired made the check's next admission answer 'previous observation worker
    still completing', and the report step logged that as ERROR every time. The
    reporter now runs inside the sample's own worker pass, so the round reaches
    no second admission that could race."""
    entered, release_probe = Event(), Event()
    tail_entered, release_tail = Event(), Event()

    def probe() -> DaemonProbe:
        entered.set()
        release_probe.wait(2)
        return DaemonProbe.up("fresh sample")

    reported: list[DaemonProbe] = []
    monitor = DiagnosticMonitor(
        [Diagnostic("held", probe, interval_s=0.001, timeout_s=5.0, report=reported.append)]
    )
    runner = monitor._states["held"].runner
    caplog.set_level(logging.ERROR, logger="services.supervision.ava_root_glue.diagnostics")
    round_task = asyncio.create_task(monitor.run_round())
    try:
        await asyncio.to_thread(entered.wait, 2)
        assert entered.is_set()
        observation = runner._observation
        assert observation is not None

        def hold_tail(_future: object) -> None:
            # The worker's result is already complete; only its thread has not retired.
            tail_entered.set()
            release_tail.wait(5)

        observation.future.add_done_callback(hold_tail)
        release_probe.set()
        await asyncio.to_thread(tail_entered.wait, 2)
        await round_task
        assert [result.detail for result in reported] == ["fresh sample"]
        assert "reporting unavailable" not in caplog.text
    finally:
        release_probe.set()
        release_tail.set()
        await round_task


async def test_a_hanging_report_retains_its_check_worker_without_overlap() -> None:
    """An alert backend that hangs retains the check's single worker exactly like
    a hung probe: later rounds observe the retained worker and start no
    overlapping sample or report."""
    entered, release, done = Event(), Event(), Event()
    probe_calls = 0
    report_calls = 0

    def probe() -> DaemonProbe:
        nonlocal probe_calls
        probe_calls += 1
        return DaemonProbe.down("observed failure")

    def report(_result: DaemonProbe) -> None:
        nonlocal report_calls
        report_calls += 1
        entered.set()
        try:
            release.wait(5)
        finally:
            done.set()

    monitor = DiagnosticMonitor(
        [
            Diagnostic("held", probe, interval_s=0.001, timeout_s=0.05, report=report),
            Diagnostic("peer", lambda: DaemonProbe.up("ok"), interval_s=0.001),
        ]
    )
    try:
        await monitor.run_round()
        assert report_calls == 1
        await asyncio.to_thread(entered.wait, 2)
        await monitor.run_round()  # the retained worker admits no replacement
        assert probe_calls == 1 and report_calls == 1
        snapshot = monitor.health_snapshot()
        assert _entry(snapshot, "diagnostic:held")["last_verdict"] == "unavailable"
        assert _entry(snapshot, "diagnostic:peer")["last_verdict"] == "alive"
        release.set()
        await asyncio.to_thread(done.wait, 2)
        await asyncio.sleep(0.05)  # the retained worker retires
        await monitor.run_round()
        assert probe_calls == 2 and report_calls == 2
    finally:
        release.set()


async def test_a_report_exception_keeps_the_fresh_sample(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A report that raises fails the report, not the sample: the fresh verdict
    is recorded, and the failure is named as the report's own."""

    def broken_report(_result: DaemonProbe) -> None:
        raise RuntimeError("alert backend down")

    monitor = DiagnosticMonitor(
        [
            Diagnostic(
                "canary",
                lambda: DaemonProbe.down("observed failure"),
                interval_s=0.001,
                report=broken_report,
            )
        ]
    )
    caplog.set_level(logging.ERROR, logger="services.supervision.ava_root_glue.diagnostics")
    await monitor.run_round()
    state = _entry(monitor.health_snapshot(), "diagnostic:canary")
    assert state["last_verdict"] == "down"
    assert state["consecutive_failures"] == 1
    assert "reporting failed" in caplog.text
    assert "alert backend down" in caplog.text


async def test_unknown_never_resolves_station_alert_but_extends_the_streak(
    events: _Events,
) -> None:
    current = DaemonProbe.down("canary failed")
    reported: list[DaemonProbe] = []
    monitor = DiagnosticMonitor(
        [
            Diagnostic(
                "canary",
                lambda: current,
                interval_s=0.001,
                report=reported.append,
            )
        ]
    )
    await monitor.run_round()
    assert [record["consecutive_failures"] for record in events] == [1]
    current = DaemonProbe.unavailable("no host baseline")
    await asyncio.sleep(0.002)
    await monitor.run_round()
    assert len(reported) == 1  # unknown did not fire or recover the external alert
    state = _entry(monitor.health_snapshot(), "diagnostic:canary")
    assert state["consecutive_failures"] == 2  # the unknown extended the failure streak
    assert state["last_verdict"] == "unavailable"
    await asyncio.sleep(0.002)
    await monitor.run_round()  # a repeated unknown reports again: the condition still holds
    assert [record["consecutive_failures"] for record in events] == [1, 2, 3]


async def test_every_failing_sample_reports_and_recovery_reports_once(
    events: _Events,
) -> None:
    current = DaemonProbe.unavailable("host stalled")
    monitor = DiagnosticMonitor([Diagnostic("canary", lambda: current, interval_s=0.001)])
    for _ in range(2):
        await monitor.run_round()
        await asyncio.sleep(0.002)
    assert [record["consecutive_failures"] for record in events] == [1, 2]
    assert all(record["event"] == "root_diagnostic" for record in events)
    current = DaemonProbe.up("healthy again")
    await monitor.run_round()
    await asyncio.sleep(0.002)
    await monitor.run_round()  # a healthy sample after a healthy one is silent
    assert [record["consecutive_failures"] for record in events] == [1, 2, 0]
    assert events[-1]["verdict"] == "alive"
    current = DaemonProbe.unavailable("host stalled")
    await asyncio.sleep(0.002)
    await monitor.run_round()
    assert [record["consecutive_failures"] for record in events] == [1, 2, 0, 1]


class _NoRevival:
    """The supervisor slice a round-level fake never reaches."""

    async def restart(self, unit_id: str) -> dict[str, object]:
        raise AssertionError(f"unexpected restart of {unit_id}")

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float] | None:
        raise AssertionError(f"unexpected generation lookup for {unit_id}")

    def revival_deferral(self, unit_id: str) -> str | None:
        raise AssertionError(f"unexpected revival check for {unit_id}")

    def unit_failure_facts(self, unit_id: str) -> UnitFailureFacts:
        raise AssertionError(f"unexpected failure facts lookup for {unit_id}")


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
    events: _Events, *, machine_identity: MachineIdentity
) -> None:
    health = _Health()
    diagnostics = DiagnosticMonitor([Diagnostic("failed", lambda: DaemonProbe.down("failed"))])
    rounds = RootHealthRounds(
        health,
        diagnostics,
        clients=ClientSet(),
        image=LoadedCommit(source_root=Path(__file__).resolve().parents[4], sha=None),
        machine=machine_identity,
    )
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
    events: _Events, *, machine_identity: MachineIdentity
) -> None:
    async with asyncio.TaskGroup() as tasks:
        health = _Health()
        rounds = RootHealthRounds(
            health,
            DiagnosticMonitor([]),
            clients=ClientSet(),
            image=LoadedCommit(source_root=Path(__file__).resolve().parents[4], sha=None),
            tasks=tasks,
            machine=machine_identity,
        )
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


@pytest.mark.parametrize("sha", ["captured-root-image", None])
async def test_root_logging_owns_only_the_started_pipeline(
    sha: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    events: _Events,
    *,
    machine_identity: MachineIdentity,
) -> None:
    image = LoadedCommit(source_root=tmp_path, sha=sha)
    made: list[EventPipeline] = []

    def producer() -> EventPipeline:
        pipeline = EventPipeline(writer=lambda _events: None)
        made.append(pipeline)
        return pipeline

    clients = ClientSet(pipeline_factory=producer)
    seen: list[LoadedCommit] = []

    def initialize(
        *, name: str, producer: object, machine_reader: object, image: LoadedCommit
    ) -> None:
        assert name == "ava-root"
        assert callable(producer) and callable(machine_reader)
        assert producer() is clients.event_pipeline()
        seen.append(image)

    monkeypatch.setattr(process_logging, "init_gateway_process", initialize)
    async with asyncio.TaskGroup() as tasks:
        rounds = RootHealthRounds(
            _Health(),
            DiagnosticMonitor([]),
            clients=clients,
            image=image,
            tasks=tasks,
            machine=machine_identity,
        )
        assert made == []
        await rounds.start()
        assert seen == [image] and len(made) == 1
        await rounds.stop()
        assert made[0].stopped


async def test_logging_startup_error_stops_its_constructed_writer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, machine_identity: MachineIdentity
) -> None:
    made: list[EventPipeline] = []
    error = ValueError("root logging failed after constructing its writer")

    def producer() -> EventPipeline:
        pipeline = EventPipeline(writer=lambda _events: None)
        made.append(pipeline)
        return pipeline

    clients = ClientSet(pipeline_factory=producer)

    def initialize(**_inputs: object) -> NoReturn:
        clients.event_pipeline()
        raise error

    monkeypatch.setattr(process_logging, "init_gateway_process", initialize)
    async with asyncio.TaskGroup() as tasks:
        rounds = RootHealthRounds(
            _Health(),
            DiagnosticMonitor([]),
            clients=clients,
            image=LoadedCommit(source_root=tmp_path, sha=None),
            tasks=tasks,
            machine=machine_identity,
        )
        with pytest.raises(ValueError) as caught:
            await rounds.start()
        assert caught.value is error and len(made) == 1
        assert made[0].stopped


def test_helper_diagnostics_are_macos_only(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    monkeypatch.setattr(probes, "is_macos", lambda: False)
    machine_identity.set(role="agent-runner")
    names = {
        check.name
        for check in probes.build_diagnostics(
            set(), database=_unused_database, machine=machine_identity
        )
    }
    assert names == {"venv", "maintenance-hold"}
    monkeypatch.setattr(probes, "is_macos", lambda: True)
    monkeypatch.setattr(
        probes,
        "settings",
        SimpleNamespace(
            services=SimpleNamespace(
                permissions_helper_enabled=False, maintenance_hold_alert_after_s=1800
            )
        ),
    )
    names = {
        check.name
        for check in probes.build_diagnostics(
            set(), database=_unused_database, machine=machine_identity
        )
    }
    assert names == {"venv", "brew-pin", "maintenance-hold", "permissions-helper"}


@pytest.mark.parametrize("role", ["gateway", "agent-runner"])
def test_every_root_watches_its_maintenance_hold_with_the_configured_age(
    monkeypatch: pytest.MonkeyPatch,
    events: _Events,
    tmp_path: Path,
    role: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from datetime import UTC, datetime

    from base.deploy.maintenance import pause_owner

    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")
    monkeypatch.setattr(probes, "is_macos", lambda: False)
    monkeypatch.setattr(
        probes,
        "settings",
        SimpleNamespace(
            services=SimpleNamespace(maintenance_hold_alert_after_s=60),
            data_plane=SimpleNamespace(is_remote=True),
            observability=SimpleNamespace(observability_url=""),
        ),
    )
    machine_identity.set(role=role)
    check = next(
        check
        for check in probes.build_diagnostics(
            set(), database=_unused_database, machine=machine_identity
        )
        if check.name == "maintenance-hold"
    )
    assert check.report is not None
    pause_owner.begin_maintenance("ava-restart-3", datetime(2026, 10, 12, tzinfo=UTC))
    result = check.probe()
    assert not result.alive  # the hold is far older than the 60-second configured age
    check.report(result)
    assert [
        event["holder"] for event in events if event.get("event") == "maintenance_hold_overdue"
    ] == ["ava-restart-3"]


def test_station_no_credential_is_unknown_and_does_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = probes.StationProbe(database=_unused_database)
    answers = Mock(return_value=True)
    monkeypatch.setattr(probe._module, "_station_answers", answers)
    monkeypatch.setattr(
        probes, "settings", SimpleNamespace(data_plane=SimpleNamespace(cluster_secret=""))
    )
    assert probe.probe().verdict.value == "unavailable"
    answers.assert_not_called()


def test_missing_brew_probe_is_unavailable_not_an_empty_healthy_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*_args: object, **_kwargs: object) -> NoReturn:
        raise FileNotFoundError("brew")

    monkeypatch.setattr(proc, "run_bounded", missing)
    with pytest.raises(FileNotFoundError):
        probes.brew_pins()  # ProbeRunner maps inspection failure to UNAVAILABLE.


def test_redis_acl_uses_runtime_ping_without_native_custody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.cluster import ownership
    from services.supervision.healthchecks import redis_acl

    ping = Mock()
    capture = Mock(side_effect=AssertionError("read-only health must not capture native custody"))
    monkeypatch.setattr(ownership.RedisConnectionCustody, "capture", capture)
    monkeypatch.setattr(redis_acl, "ping", ping)
    monkeypatch.setattr(
        probes,
        "settings",
        SimpleNamespace(data_plane=SimpleNamespace(redis_url="redis://localhost:6380")),
    )
    assert probes.redis_acl().alive
    ping.assert_called_once_with("redis://localhost:6380")
    capture.assert_not_called()


def test_each_root_roster_owns_its_helper_episode_reporter(
    monkeypatch: pytest.MonkeyPatch,
    events: _Events,
    *,
    machine_identity: MachineIdentity,
) -> None:
    monkeypatch.setattr(probes, "is_macos", lambda: True)
    machine_identity.set(role="agent-runner")
    first = next(
        check
        for check in probes.build_diagnostics(
            set(), database=_unused_database, machine=machine_identity
        )
        if check.name == "permissions-helper"
    )
    second = next(
        check
        for check in probes.build_diagnostics(
            set(), database=_unused_database, machine=machine_identity
        )
        if check.name == "permissions-helper"
    )
    assert first.report is not None and second.report is not None
    bad = DaemonProbe.down("lwcr-stuck; helper unavailable")
    first.report(bad)
    first.report(bad)
    second.report(bad)
    assert (
        len([event for event in events if event.get("event") == "permissions_helper_unhealthy"])
        == 2
    )
