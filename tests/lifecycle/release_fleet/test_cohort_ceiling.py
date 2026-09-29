"""The frozen cohort's ceiling is what one release journal can carry.

A whole-cohort failure journals a few ids per agent (cohort, reaped turns,
the drain's alert, window sightings, the threshold and recovering alerts)
plus one affected entry. Past what the journal holds, the verdict cannot be
journaled and a failing release holds at `watching` instead of recovering,
so a cohort above `MAX_COHORT_AGENTS` is refused where it is frozen: the
release aborts before the fence and restarts the previous image unchanged.
"""

from __future__ import annotations

from datetime import datetime

from base.deploy.maintenance.state import MaintenanceHold
from cli.release_fleet.gateway import Samples
from cli.release_fleet.policy import MAX_COHORT_AGENTS, Cohort
from cli.release_fleet.request import FleetRequest
from cli.release_transition.journal import Operation, create, exclusive, read_operation
from tests.lifecycle.release_fleet.fakes import drive
from tests.lifecycle.release_fleet.test_coordinator import Effects
from tests.lifecycle.release_fleet.test_coordinator import request_record as request_record

# Wider agent ids cost more journal bytes; production ids are far below this.
_FIRST_ID = 1_000_000


class WholeCohortFailure(Effects):
    """Every agent's turn is reaped by the drain, then every agent is sighted
    with a runtime error and a quarantine in the candidate's watch window."""

    def __init__(self, request: FleetRequest, size: int) -> None:
        super().__init__(request)
        self.cohort_agents = tuple(range(_FIRST_ID, _FIRST_ID + size))

    def quiesce(self, operation: Operation) -> MaintenanceHold:
        assert operation == self._effect("quiescing")
        agents = self.cohort_agents
        return MaintenanceHold(
            phase="drained",
            commands=dict.fromkeys(agents, 1),
            reaped=dict.fromkeys(agents, "reaped"),
        )

    def sample(
        self,
        operation: Operation,
        cohort: Cohort,
        *,
        since: datetime,
        observed: datetime,
        agents: bool,
    ) -> Samples:
        samples = super().sample(operation, cohort, since=since, observed=observed, agents=agents)
        if operation.phase != "watching" or operation.direction != "candidate":
            return samples
        failing = tuple(
            report.model_copy(update={"live": False, "runtime_error": True, "quarantined": True})
            for report in samples.agents
        )
        return samples._replace(agents=failing)


def _drive(request: FleetRequest, effects: Effects) -> Operation:
    create(request)
    with exclusive(request.path) as journal:
        drive(journal, effects)
    return read_operation(request.path)


def test_a_whole_cohort_failure_at_the_ceiling_still_recovers(
    request_record: FleetRequest,
) -> None:
    final = _drive(request_record, WholeCohortFailure(request_record, MAX_COHORT_AGENTS))

    assert final.error is None and final.fleet is not None
    assert final.fleet.outcome == "recovered"
    assert final.fleet.cohort is not None and final.fleet.cohort.size == MAX_COHORT_AGENTS
    [recover] = final.fleet.decisions
    assert (recover.kind, recover.phase) == ("recover", "watching")


def test_a_cohort_above_the_ceiling_aborts_before_the_fence(
    request_record: FleetRequest,
) -> None:
    effects = Effects(request_record)
    effects.cohort_agents = tuple(range(_FIRST_ID, _FIRST_ID + MAX_COHORT_AGENTS + 1))

    final = _drive(request_record, effects)

    assert final.fleet is not None and final.fleet.outcome == "aborted"
    assert final.fleet.cohort is None
    [abort] = final.fleet.decisions
    assert (abort.kind, abort.phase) == ("abort", "quiescing")
    assert f"{MAX_COHORT_AGENTS + 1} agents" in abort.reason
    assert f"ceiling of {MAX_COHORT_AGENTS}" in abort.reason
    assert [phase for phase, _direction in effects.events] == ["prepared", "quiescing", "restoring"]
