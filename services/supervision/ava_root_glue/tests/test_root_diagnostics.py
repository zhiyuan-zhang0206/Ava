"""Root observations must distinguish evidence, freshness, and recovery authority."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import NoReturn, cast
from unittest.mock import Mock

import pytest

from base.agents.context.clients import ClientSet
from base.daemon.health import DaemonProbe
from base.native_process.loaded_commit import LoadedCommit
from base.native_process.ownership import OwnedProcess
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


class _EventLog:
    """Structured log calls captured as their field dictionaries."""

    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def info(self, *_args: object, **fields: object) -> None:
        self._records.append(fields)

    warning = info
    error = info
    log = info


def _unused_database() -> NoReturn:
    raise AssertionError("this diagnostic must not construct a database")


def _no_op(**_kwargs: object) -> None:
    return None


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    monkeypatch.setattr("base.log.init_gateway_process", _no_op)
    monkeypatch.setattr("base.telemetry.sync", _no_op)
    monkeypatch.setattr("base.log.logger", _EventLog(records))
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


async def test_unknown_never_resolves_station_alert_but_extends_the_streak(
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
    events: list[dict[str, object]],
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
    events: list[dict[str, object]],
) -> None:
    health = _Health()
    diagnostics = DiagnosticMonitor([Diagnostic("failed", lambda: DaemonProbe.down("failed"))])
    rounds = RootHealthRounds(
        health,
        diagnostics,
        clients=ClientSet(),
        image=LoadedCommit(source_root=Path(__file__).resolve().parents[4], sha=None),
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
    events: list[dict[str, object]],
) -> None:
    async with asyncio.TaskGroup() as tasks:
        health = _Health()
        rounds = RootHealthRounds(
            health,
            DiagnosticMonitor([]),
            clients=ClientSet(),
            image=LoadedCommit(source_root=Path(__file__).resolve().parents[4], sha=None),
            tasks=tasks,
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
    events: list[dict[str, object]],
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

    monkeypatch.setattr("base.log.init_gateway_process", initialize)
    async with asyncio.TaskGroup() as tasks:
        rounds = RootHealthRounds(
            _Health(), DiagnosticMonitor([]), clients=clients, image=image, tasks=tasks
        )
        assert made == []
        await rounds.start()
        assert seen == [image] and len(made) == 1
        await rounds.stop()
        assert made[0].stopped


async def test_logging_startup_error_stops_its_constructed_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    monkeypatch.setattr("base.log.init_gateway_process", initialize)
    async with asyncio.TaskGroup() as tasks:
        rounds = RootHealthRounds(
            _Health(),
            DiagnosticMonitor([]),
            clients=clients,
            image=LoadedCommit(source_root=tmp_path, sha=None),
            tasks=tasks,
        )
        with pytest.raises(ValueError) as caught:
            await rounds.start()
        assert caught.value is error and len(made) == 1
        assert made[0].stopped


def test_helper_diagnostics_are_macos_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probes, "is_macos", lambda: False)
    monkeypatch.setattr("base.cluster.machine.is_gateway", lambda: False)
    names = {check.name for check in probes.build_diagnostics(set(), database=_unused_database)}
    assert names == {"venv"}
    monkeypatch.setattr(probes, "is_macos", lambda: True)
    monkeypatch.setattr(
        probes,
        "settings",
        SimpleNamespace(services=SimpleNamespace(permissions_helper_enabled=False)),
    )
    names = {check.name for check in probes.build_diagnostics(set(), database=_unused_database)}
    assert names == {"venv", "brew-pin", "permissions-helper"}


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

    monkeypatch.setattr("base.host.proc.run_bounded", missing)
    with pytest.raises(FileNotFoundError):
        probes.brew_pins()  # ProbeRunner maps inspection failure to UNAVAILABLE.


def test_redis_acl_uses_runtime_ping_without_native_custody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.cluster import ownership
    from services.supervision.healthchecks import redis_acl

    ping = Mock()
    capture = Mock(side_effect=AssertionError("read-only health must not capture native custody"))
    monkeypatch.setattr(ownership, "configured_redis_port", Mock(return_value=6380))
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
    monkeypatch: pytest.MonkeyPatch, events: list[dict[str, object]]
) -> None:
    monkeypatch.setattr(probes, "is_macos", lambda: True)
    monkeypatch.setattr("base.cluster.machine.is_gateway", lambda: False)
    first = next(
        check
        for check in probes.build_diagnostics(set(), database=_unused_database)
        if check.name == "permissions-helper"
    )
    second = next(
        check
        for check in probes.build_diagnostics(set(), database=_unused_database)
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
