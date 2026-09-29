"""A coordinator harness: a gateway unit with no cluster side effects and a fake clock.

`OffDutyGateway` keeps the gateway unit's release effects overridable (each
test replaces the ones it exercises) and answers the coordinator's own needs
in memory: the deploy lease, evidence samples (all healthy unless a test says
otherwise), the fleet release identity, publication and alert deliveries.
`Clock` advances only when the coordinator sleeps, so a watch window elapses
in a few iterations without real time.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from base.deploy.maintenance.state import MaintenanceHold
from cli.release_fleet.coordinator import Coordinator
from cli.release_fleet.gateway import GatewayUnit, Samples
from cli.release_fleet.policy import AlertRoute, Cohort
from cli.release_fleet.progress import AlertRecord
from cli.release_fleet.publication import Completion, FleetRelease
from cli.release_fleet.request import FleetRequest, fleet_release
from cli.release_fleet.units import RemoteUnits
from cli.release_fleet.workload import CORE_SIGNALS, AgentReport, CoreReport, UnitReport
from cli.release_transition.authority_evidence import GenerationRef
from cli.release_transition.journal import Journal, Operation
from cli.release_transition.request import ReleaseRef
from tests.lifecycle.transition.phases import generation


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeLease:
    """The deploy lease's calls, recorded: `held` once while held, then `released`."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.lost = False

    def hold(self) -> None:
        if "held" not in self.events or self.events[-1] == "released":
            self.events.append("held")

    def require(self) -> None:
        if self.lost:
            raise RuntimeError("the fleet operation lost its cluster deploy lease")

    def release(self) -> None:
        self.events.append("released")


class OffDutyGateway(GatewayUnit):
    """Every effect is a no-op; override the ones a test exercises."""

    def __init__(self, request: FleetRequest) -> None:
        self.request = request
        self.home = Path(request.home)
        self.lease = FakeLease()  # type: ignore[assignment]  # the lease's three calls, recorded
        self.published: list[Completion] = []
        self.delivered: list[tuple[str, str]] = []
        self.failing_signals: dict[str, str] = {}
        self.dead_agents: set[int] = set()
        self.cohort_agents: tuple[int, ...] = ()
        self.undeliverable: set[str] = set()

    # the coordinator's own needs ─────────────────────────────────────────

    def release_of(self, reference: ReleaseRef) -> FleetRelease:
        return fleet_release(reference, "0" * 64)

    def publish(self, completion: Completion) -> None:
        if all(item.operation != completion.operation for item in self.published):
            self.published.append(completion)

    def deliver(self, record: AlertRecord, route: AlertRoute) -> tuple[str, ...]:
        del route
        if record.alert.kind in self.undeliverable:
            return ()
        self.delivered.append((record.alert.kind, "alert_row"))
        return ("alert_row",)

    def sample(
        self,
        operation: Operation,
        cohort: Cohort,
        *,
        since: datetime,
        observed: datetime,
        agents: bool,
    ) -> Samples:
        del operation, since
        core = tuple(
            CoreReport(
                signal=signal,
                ok=signal not in self.failing_signals,
                observed_at=observed,
                detail=self.failing_signals.get(signal),
            )
            for signal in CORE_SIGNALS
        )
        gateway = UnitReport(unit=self.request.gateway, state="ready", observed_at=observed)
        sampled = tuple(
            AgentReport(
                agent=agent,
                live=agent not in self.dead_agents,
                runtime_error=False,
                quarantined=False,
                observed_at=observed,
            )
            for agent in cohort.members
        )
        return Samples(gateway=gateway, core=core, agents=sampled if agents else ())

    # release effects ─────────────────────────────────────────────────────

    def preflight(self) -> None:
        return

    def preflight_authority(self) -> GenerationRef:
        return generation(0)

    def quiesce(self, operation: Operation) -> MaintenanceHold:
        del operation
        return MaintenanceHold(phase="drained", commands=dict.fromkeys(self.cohort_agents, 1))

    def stop(self, operation: Operation) -> None:
        return

    def select(self, operation: Operation) -> None:
        return

    def start(self, journal: Journal) -> None:
        return

    def observe(self, operation: Operation) -> None:
        return

    def resume(self, operation: Operation) -> None:
        return

    def restore(self, journal: Journal) -> None:
        return


def drive(
    journal: Journal,
    gateway: GatewayUnit,
    *,
    clock: Clock | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Run the coordinator for this fleet of one until it completes or holds."""
    request = journal.operation.request
    assert isinstance(request, FleetRequest)
    ticking = clock or Clock(request.created_at + timedelta(seconds=1))
    Coordinator(
        journal,
        gateway,
        RemoteUnits(request),
        clock=ticking,
        sleep=sleep or ticking.sleep,
    ).run()
