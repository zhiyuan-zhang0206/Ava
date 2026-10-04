"""Failure-state signals: `root_unit_failure_state` and `root_unit_not_revivable`.

Both are STATE signals, so the health monitor emits them on every round while
the condition holds; the observability rules own the debounce and the paging.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from base.daemon.health import DaemonProbe
from base.native_process.ownership import OwnedProcess
from services.ava_root import health as health_mod
from services.ava_root.custody import ReconcileOutcome
from services.ava_root.failure_state import (
    FailureKind,
    UnitFailureFacts,
    UnitFailureView,
    derive_kind,
    describe,
)
from services.ava_root.health import HealthConfig, HealthMonitor
from services.ava_root.intent_store import RestartFailure, RestartStage
from services.ava_root.probes import ProbeRegistry


class _Recorder:
    """Stands in for base.log.logger; records every structured call with its level."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def info(self, message: str, **extra: object) -> None:
        self.calls.append({"message": message, "level": "info", **extra})

    def warning(self, message: str, **extra: object) -> None:
        self.calls.append({"message": message, "level": "warning", **extra})

    def events(self, name: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c.get("event") == name]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    import base.log as base_log

    rec = _Recorder()
    monkeypatch.setattr(base_log, "logger", rec)
    return rec


def _failure(stage: RestartStage = RestartStage.UP, detail: str = "boom") -> RestartFailure:
    return RestartFailure(stage=stage, since=12.0, detail=detail)


def _view(
    *,
    intent_running: bool = True,
    restart_failed: RestartFailure | None = None,
    breaker_open: bool = False,
    custody_held: bool = False,
    detail: str = "",
) -> UnitFailureView:
    return UnitFailureView(
        unit="svc",
        facts=UnitFailureFacts(
            intent_running=intent_running,
            restart_failed=restart_failed,
            custody_held=custody_held,
        ),
        breaker_open=breaker_open,
        detail=detail,
    )


def test_derive_kind_priority_and_the_intent_gate() -> None:
    failure = _failure()
    assert derive_kind(_view()) is None
    assert derive_kind(_view(breaker_open=True)) is FailureKind.BREAKER_OPEN
    assert derive_kind(_view(custody_held=True)) is FailureKind.CUSTODY_HELD
    assert derive_kind(_view(restart_failed=failure)) is FailureKind.RESTART_FAILED
    # The recorded replacement failure wins over the breaker, which wins over custody.
    both = _view(restart_failed=failure, breaker_open=True, custody_held=True)
    assert derive_kind(both) is FailureKind.RESTART_FAILED
    assert derive_kind(_view(breaker_open=True, custody_held=True)) is FailureKind.BREAKER_OPEN
    # A stop holds every failure state and still never reports.
    stopped = _view(
        intent_running=False, restart_failed=failure, breaker_open=True, custody_held=True
    )
    assert derive_kind(stopped) is None


def test_describe_carries_the_kind_evidence() -> None:
    assert describe(
        _view(restart_failed=_failure(RestartStage.DOWN, "stop refused")),
        FailureKind.RESTART_FAILED,
    ) == ("replacement failed at its down half: stop refused")
    assert describe(_view(detail="no probe"), FailureKind.BREAKER_OPEN) == "no probe"
    assert describe(_view(), FailureKind.BREAKER_OPEN) == "restart breaker open"
    assert "reconciliation" in describe(_view(), FailureKind.CUSTODY_HELD)


class _Supervisor:
    """RevivalHost stand-in for one unit with flippable failure facts."""

    def __init__(self) -> None:
        self.generation = (OwnedProcess(42, 100.0, None), 0.0)
        self.facts = UnitFailureFacts(intent_running=True, restart_failed=None, custody_held=False)

    async def restart(self, unit_id: str) -> dict[str, object]:
        return {"verb": "restart", "units": []}

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float]:
        return self.generation

    def revival_deferral(self, unit_id: str) -> str | None:
        return None

    def unit_failure_facts(self, unit_id: str) -> UnitFailureFacts:
        return self.facts

    async def reconcile_custody(self) -> list[ReconcileOutcome]:
        return []


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
    monkeypatch.setattr(health_mod, "_monotonic", fake)
    return fake


def _monitor(supervisor: _Supervisor, probe: Callable[[], DaemonProbe]) -> HealthMonitor:
    registry = ProbeRegistry()
    registry.register("svc", probe)
    values: dict[str, Any] = {
        "interval_s": 0.01,
        "startup_grace_s": 0.0,
        "verify_deadline_s": 0.05,
        "verify_interval_s": 0.001,
        "failures_before_restart": 1,
        "backoff_base_s": 600.0,
        "backoff_cap_s": 600.0,
        "breaker_rounds": 3,
    }
    return HealthMonitor(supervisor, registry, config=HealthConfig(**values))


async def test_breaker_state_is_emitted_every_round_it_holds_then_stops(
    clock: _Clock, recorder: _Recorder
) -> None:
    probe = _Cell(DaemonProbe.down("no"))
    monitor = _monitor(_Supervisor(), probe)
    await monitor.run_round()  # 1: restart attempt, not verified
    await monitor.run_round()  # 2: backing off
    assert recorder.events("root_unit_failure_state") == []
    await monitor.run_round()  # 3: the breaker opens
    await monitor.run_round()  # 4: held
    states = recorder.events("root_unit_failure_state")
    assert [(s["unit"], s["kind"], s["level"]) for s in states] == [
        ("svc", "breaker_open", "warning"),
        ("svc", "breaker_open", "warning"),
    ]
    assert len(recorder.events("root_restart_breaker_open")) == 1  # the edge stays one
    probe.verdict = DaemonProbe.up("ok")
    await monitor.run_round()  # alive: the breaker closes and the signal stops
    assert len(recorder.events("root_unit_failure_state")) == 2


async def test_recorded_restart_failure_and_custody_are_state_signals(
    clock: _Clock, recorder: _Recorder
) -> None:
    supervisor = _Supervisor()
    monitor = _monitor(supervisor, _Cell(DaemonProbe.up("ok")))
    supervisor.facts = UnitFailureFacts(
        intent_running=True,
        restart_failed=_failure(RestartStage.DOWN, "stop refused"),
        custody_held=False,
    )
    await monitor.run_round()
    await monitor.run_round()
    supervisor.facts = UnitFailureFacts(intent_running=True, restart_failed=None, custody_held=True)
    await monitor.run_round()
    supervisor.facts = UnitFailureFacts(
        intent_running=False, restart_failed=None, custody_held=True
    )
    await monitor.run_round()  # an operator stop is expected, never a failure state
    states = recorder.events("root_unit_failure_state")
    assert [s["kind"] for s in states] == ["restart_failed", "restart_failed", "custody_held"]
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
