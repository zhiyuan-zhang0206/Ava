"""Builders for the pure fleet workload-policy tests.

Every time is a fixed instant relative to `RESUMED`; nothing reads a clock.
A two-unit fleet: the gateway unit and one runner unit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from cli.release_fleet.policy import Cohort, FleetPolicy, UnitCohort, UnitKey, capture_cohort
from cli.release_fleet.workload import CORE_SIGNALS, AgentReport, CoreReport, Evidence, UnitReport

RESUMED = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
GATEWAY = UnitKey(machine="ubuntu", home="/home/zzy/.ava")
RUNNER = UnitKey(machine="macbook-air", home="/Users/zzy/.ava")
OPERATION = UUID("00000000-0000-4000-8000-000000000001")
POLICY = FleetPolicy()
WATCH_END = RESUMED + timedelta(seconds=POLICY.watch_s)


def at(seconds: float, base: datetime = RESUMED) -> datetime:
    return base + timedelta(seconds=seconds)


def two_units(
    gateway_agents: tuple[int, ...] = (),
    runner_agents: tuple[int, ...] = (),
    reaped: tuple[int, ...] = (),
) -> Cohort:
    return capture_cohort(
        gateway=GATEWAY,
        reports=(
            UnitCohort(
                unit=RUNNER,
                agents=runner_agents,
                reaped=tuple(a for a in reaped if a in runner_agents),
            ),
            UnitCohort(
                unit=GATEWAY,
                agents=gateway_agents,
                reaped=tuple(a for a in reaped if a in gateway_agents),
            ),
        ),
        captured_at=at(-600),
    )


def core_ok(when: datetime) -> tuple[CoreReport, ...]:
    return tuple(CoreReport(signal=signal, ok=True, observed_at=when) for signal in CORE_SIGNALS)


def units_ready(when: datetime, *units: UnitKey) -> tuple[UnitReport, ...]:
    chosen = units or (GATEWAY, RUNNER)
    return tuple(UnitReport(unit=unit, state="ready", observed_at=when) for unit in chosen)


def agents_live(when: datetime, agents: tuple[int, ...]) -> tuple[AgentReport, ...]:
    return tuple(
        AgentReport(
            agent=agent, live=True, runtime_error=False, quarantined=False, observed_at=when
        )
        for agent in agents
    )


def healthy(when: datetime, agents: tuple[int, ...]) -> Evidence:
    """Everything sampled ok at `when`, for the given live agents."""
    return Evidence(
        since=RESUMED,
        units=units_ready(when),
        agents=agents_live(when, agents),
        core=core_ok(when),
    )
