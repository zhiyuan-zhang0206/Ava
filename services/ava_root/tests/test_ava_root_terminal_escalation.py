"""The NOT REVIVABLE escalation policy — `services.ava_root.health` (2026-10-03, E7).

A single terminal verdict (PORT_TAKEN / UNAVAILABLE — an inspection that failed
or a foreign holder) is usually a one-round race; only a consecutive streak
escalates from WARNING to ERROR, and any non-terminal round re-arms the count.
This module exists beside `test_ava_root_health.py` because that file sits at
its frozen size ceiling.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest

from base.daemon.health import DaemonProbe
from base.native_process.ownership import OwnedProcess
from services.ava_root import health as health_mod
from services.ava_root.alerts import UnitAlertFacts
from services.ava_root.custody import ReconcileOutcome
from services.ava_root.health import HealthConfig, HealthMonitor
from services.ava_root.probes import ProbeRegistry


class StubSupervisor:
    """RevivalHost stand-in: the slice a terminal round touches."""

    def __init__(self) -> None:
        self.restart_calls: list[str] = []
        self.generation = (OwnedProcess(42, 100.0, None), 0.0)
        self.alert_facts = UnitAlertFacts(
            intent_running=True, restart_failed=None, custody_held=False
        )

    async def restart(self, unit_id: str) -> dict[str, object]:
        self.restart_calls.append(unit_id)
        return {"verb": "restart", "units": []}

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float]:
        return self.generation

    def revival_deferral(self, unit_id: str) -> str | None:
        return None

    def unit_alert_facts(self, unit_id: str) -> UnitAlertFacts:
        return self.alert_facts

    async def reconcile_custody(self) -> list[ReconcileOutcome]:
        return []


class CellProbe:
    """A probe returning one flippable verdict."""

    def __init__(self, verdict: DaemonProbe) -> None:
        self.verdict = verdict

    def __call__(self) -> DaemonProbe:
        return self.verdict


class FakeClock:
    """The `_monotonic` seam: advances a fixed step per call."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 0.02
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(health_mod, "_monotonic", fake)
    return fake


def _registry(unit_id: str, probe: Callable[[], DaemonProbe]) -> ProbeRegistry:
    registry = ProbeRegistry()
    registry.register(unit_id, probe)
    return registry


def _config(**overrides: Any) -> HealthConfig:
    values: dict[str, Any] = {
        "interval_s": 0.01,
        "startup_grace_s": 0.0,
        "verify_deadline_s": 0.05,
        "verify_interval_s": 0.001,
        "failures_before_restart": 1,
        "backoff_base_s": 30.0,
        "backoff_cap_s": 120.0,
        "breaker_rounds": 3,
    }
    values.update(overrides)
    return HealthConfig(**values)


def _not_revivable_levels(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.levelname for r in caplog.records if "NOT REVIVABLE" in r.getMessage()]


async def test_terminal_escalates_only_after_consecutive_rounds(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.ava_root.health")
    probe = CellProbe(DaemonProbe.port_taken("alien daemon"))
    stub = StubSupervisor()
    monitor = HealthMonitor(stub, _registry("svc", probe), config=_config())

    await monitor.run_round()  # terminal round 1: a flake, not an incident
    assert _not_revivable_levels(caplog) == ["WARNING"]
    assert monitor.snapshot()["svc"].terminal_rounds == 1
    assert monitor.health_snapshot()["svc"]["terminal_rounds"] == 1  # type: ignore[index]
    assert stub.restart_calls == []  # terminal never restarts

    await monitor.run_round()  # terminal round 2: genuinely unresolvable
    assert _not_revivable_levels(caplog) == ["WARNING", "ERROR"]

    # A non-terminal round re-arms the escalation: the next terminal round is
    # a lone flake again.
    probe.verdict = DaemonProbe.up("healthy again")
    await monitor.run_round()
    assert monitor.snapshot()["svc"].terminal_rounds == 0
    probe.verdict = DaemonProbe.unavailable("inspection raced")
    await monitor.run_round()
    assert _not_revivable_levels(caplog) == ["WARNING", "ERROR", "WARNING"]


def test_terminal_escalate_rounds_must_be_positive() -> None:
    with pytest.raises(ValueError, match="terminal_escalate_rounds"):
        HealthConfig(terminal_escalate_rounds=0)
