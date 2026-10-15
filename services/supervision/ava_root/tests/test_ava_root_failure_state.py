from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from base.daemon.health import DaemonProbe
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.ownership import OwnedProcess
from services.supervision.ava_root import health as health_mod
from services.supervision.ava_root.failure_state import (
    FailureKind,
    UnitFailureFacts,
    derive_kind,
    describe,
)
from services.supervision.ava_root.health import HealthConfig, HealthMonitor
from services.supervision.ava_root.intent_store import RestartFailure, RestartStage
from services.supervision.ava_root.probes import ProbeRegistry


class _Recorder:
    def __init__(self, records: LogRecords) -> None:
        self._records = records

    @property
    def calls(self) -> list[dict[str, object]]:
        return [
            {"message": r["message"], "level": r["level"].name.lower(), **r["extra"]}
            for r in self._records
        ]

    def events(self, name: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c.get("event") == name]


@pytest.fixture
def recorder(loguru_records: LogRecords) -> _Recorder:
    return _Recorder(loguru_records)


def _failure(stage: RestartStage = RestartStage.UP, detail: str = "boom") -> RestartFailure:
    return RestartFailure(stage=stage, since=12.0, detail=detail)


def test_derive_kind_priority_and_the_intent_gate() -> None:
    facts = UnitFailureFacts(intent_running=True, restart_failed=None)
    assert derive_kind(facts, restart_limit_reached=False) is None
    assert derive_kind(facts, restart_limit_reached=True) is FailureKind.RESTARTS_EXHAUSTED
    failed = UnitFailureFacts(intent_running=True, restart_failed=_failure())
    assert derive_kind(failed, restart_limit_reached=True) is FailureKind.RESTART_FAILED
    stopped = UnitFailureFacts(intent_running=False, restart_failed=_failure())
    assert derive_kind(stopped, restart_limit_reached=True) is None


def test_describe_carries_the_kind_evidence() -> None:
    failed = UnitFailureFacts(
        intent_running=True, restart_failed=_failure(RestartStage.DOWN, "stop refused")
    )
    assert describe(failed, FailureKind.RESTART_FAILED) == (
        "replacement failed at its down half: stop refused"
    )
    facts = UnitFailureFacts(intent_running=True, restart_failed=None)
    assert describe(facts, FailureKind.RESTARTS_EXHAUSTED, "no probe") == "no probe"
    assert describe(facts, FailureKind.RESTARTS_EXHAUSTED) == "automatic restart attempts exhausted"


class _Supervisor:
    def __init__(self) -> None:
        self.generation = (OwnedProcess(42, 100.0, None), 0.0)
        self.facts = UnitFailureFacts(intent_running=True, restart_failed=None)

    async def restart(self, unit_id: str) -> dict[str, object]:
        return {"verb": "restart", "units": []}

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float]:
        return self.generation

    def revival_deferral(self, unit_id: str) -> str | None:
        return None

    def unit_failure_facts(self, unit_id: str) -> UnitFailureFacts:
        return self.facts


class _Cell:
    def __init__(self, verdict: DaemonProbe) -> None:
        self.verdict = verdict

    def __call__(self) -> DaemonProbe:
        return self.verdict


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 0.02
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(health_mod, "monotonic", fake)
    return fake


def _monitor(supervisor: _Supervisor, probe: Callable[[], DaemonProbe]) -> HealthMonitor:
    registry = ProbeRegistry()
    registry.register("svc", probe)
    values: dict[str, Any] = {
        "interval_s": 0.01,
        "startup_grace_s": 0.0,
        "max_restart_attempts": 3,
    }
    return HealthMonitor(supervisor, registry, config=HealthConfig(**values))


async def test_exhausted_attempts_are_reported_every_round_then_clear_on_alive(
    clock: _Clock, recorder: _Recorder
) -> None:
    probe = _Cell(DaemonProbe.down("no"))
    monitor = _monitor(_Supervisor(), probe)
    await monitor.run_round()
    await monitor.run_round()
    assert recorder.events("root_unit_failure_state") == []
    await monitor.run_round()
    await monitor.run_round()
    states = recorder.events("root_unit_failure_state")
    assert [(s["unit"], s["kind"], s["level"]) for s in states] == [
        ("svc", "restarts_exhausted", "warning"),
        ("svc", "restarts_exhausted", "warning"),
    ]
    probe.verdict = DaemonProbe.up("ok")
    await monitor.run_round()
    assert len(recorder.events("root_unit_failure_state")) == 2


async def test_recorded_restart_failure_is_a_state_signal(
    clock: _Clock, recorder: _Recorder
) -> None:
    supervisor = _Supervisor()
    monitor = _monitor(supervisor, _Cell(DaemonProbe.up("ok")))
    supervisor.facts = UnitFailureFacts(
        intent_running=True,
        restart_failed=_failure(RestartStage.DOWN, "stop refused"),
    )
    await monitor.run_round()
    await monitor.run_round()
    supervisor.facts = UnitFailureFacts(intent_running=True, restart_failed=None)
    await monitor.run_round()
    supervisor.facts = UnitFailureFacts(intent_running=False, restart_failed=None)
    await monitor.run_round()
    states = recorder.events("root_unit_failure_state")
    assert [s["kind"] for s in states] == ["restart_failed", "restart_failed"]
    assert states[0]["detail"] == "replacement failed at its down half: stop refused"


async def test_terminal_verdict_is_reported_every_round_at_warning(
    clock: _Clock, recorder: _Recorder
) -> None:
    probe = _Cell(DaemonProbe.port_taken("alien daemon"))
    monitor = _monitor(_Supervisor(), probe)
    await monitor.run_round()
    await monitor.run_round()
    reports = recorder.events("root_unit_not_revivable")
    assert [(r["unit"], r["detail"], r["level"]) for r in reports] == [
        ("svc", "alien daemon", "warning"),
        ("svc", "alien daemon", "warning"),
    ]
    probe.verdict = DaemonProbe.up("healthy again")
    await monitor.run_round()
    assert len(recorder.events("root_unit_not_revivable")) == 2
