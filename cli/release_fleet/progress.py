"""Durable fleet progress: what a fleet or unit operation journals beside its phase.

The fleet journal is the gateway home's `Operation` (kind `fleet`), and a
remote unit's journal is that unit's `Operation` (kind `unit`); these records
are their fleet-specific progress, written only through the home journal
(`Journal.record_fleet`, `Journal.abort` / `recover`, `Journal.complete`), so
intent precedes effect and every history (instructions acted on, verdicts,
alerts, decisions) only grows. Pure: no clock, file or network access.

Phases reuse the one-home names where the effect is the same: `stopping` is the
plan's `closing`, `starting` + `observing` its `starting_gateway`. A fleet of
one runs every phase; `dispatching` and `starting_units` have no remote unit
to wait for.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from cli.release_fleet.alerting import FleetAlert, deliveries
from cli.release_fleet.policy import AlertRoute, Cohort, UnitCohort, UnitKey
from cli.release_fleet.request import FleetRequest, UnitRequest
from cli.release_fleet.workload import AgentReport, Verdict
from cli.release_transition.authority_evidence import GenerationRef
from cli.release_transition.request import Digest, Record
from shared.cluster.authority.model import Direction

_FLEET_CANDIDATE = (
    "prepared",
    "dispatching",
    "quiescing",
    "stopping",
    "fencing",
    "selecting",
    "authorizing",
    "starting",
    "observing",
    "starting_units",
    "resuming",
    "watching",
    "complete",
)
# A recovery re-quiesces only when admission had reopened (from `watching`).
_FLEET_PREVIOUS = (*_FLEET_CANDIDATE[2:11], "complete")
# A unit never fences; it takes its capability before selecting (dbgen-8).
_UNIT_CANDIDATE = (
    "prepared",
    "quiescing",
    "stopping",
    "authorizing",
    "selecting",
    "starting",
    "observing",
    "resuming",
    "complete",
)
_UNIT_PREVIOUS = _UNIT_CANDIDATE[1:]
FLEET_PHASES = frozenset({*_FLEET_CANDIDATE, "restoring"})
UNIT_PHASES = frozenset({*_UNIT_CANDIDATE, "restoring"})
# Before the database fence a failure aborts: every unit restarts its
# unchanged previous image on the unchanged generation.
FLEET_ABORTABLE = frozenset({"prepared", "dispatching", "quiescing", "stopping"})
UNIT_ABORTABLE = frozenset({"prepared", "quiescing", "stopping"})
# After the fence a candidate failure recovers once. Before admission reopens
# the same maintenance hold is reused; from `watching` a new hold drains again.
FLEET_RECOVERY = {
    "starting": "stopping",
    "observing": "stopping",
    "starting_units": "stopping",
    "watching": "quiescing",
}
UNIT_RECOVERY = {
    "stopping": "stopping",
    "authorizing": "stopping",
    "selecting": "stopping",
    "starting": "stopping",
    "observing": "stopping",
    "resuming": "quiescing",
}

Outcome = Literal["clean", "degraded", "aborted", "recovered"]
InstructionAction = Literal[
    "standby",  # dispatched; wait for quiescing
    "quiesce",  # drain local agents, report the cohort
    "close",  # stop root and every local writer
    "wait",  # hold while the coordinator fences, selects and authorizes
    "start",  # take the capability, select, start, observe
    "resume",  # release the local hold
    "watch",  # keep serving; keep the hold identity
    "restore",  # abort: restart the unchanged previous root on the unchanged generation
    "complete",  # finish the unit operation
    "excluded",  # close and stay closed until a converge
]
ReportState = Literal[
    "dispatched", "drained", "closed", "ready", "resumed", "restored", "completed", "failed"
]
_ANSWERS: dict[InstructionAction, frozenset[ReportState]] = {
    "standby": frozenset({"dispatched"}),
    "quiesce": frozenset({"drained"}),
    "close": frozenset({"closed"}),
    "wait": frozenset({"closed"}),
    "start": frozenset({"ready"}),
    "resume": frozenset({"resumed"}),
    "watch": frozenset({"resumed"}),
    "restore": frozenset({"restored"}),
    "complete": frozenset({"completed"}),
    "excluded": frozenset({"closed"}),
}
_MAX_EVIDENCE_BYTES = 16 * 1024


def decision_target(
    kind: Literal["fleet", "unit"],
    phase: str,
    decision: Literal["abort", "recover"],
    *,
    renewed: bool,
) -> str:
    """The phase an abort or a recovery continues from; a refusal when it cannot."""
    if decision == "abort":
        if phase not in (FLEET_ABORTABLE if kind == "fleet" else UNIT_ABORTABLE):
            raise ValueError(f"a release cannot abort from {phase}; the fence decides recovery")
        if renewed:
            raise ValueError("an abort keeps its maintenance hold")
        return "restoring"
    target = (FLEET_RECOVERY if kind == "fleet" else UNIT_RECOVERY).get(phase)
    if target is None:
        raise ValueError(f"a release cannot recover from {phase}")
    if (target == "quiescing") != renewed:
        raise ValueError("a recovery takes a new maintenance hold exactly when admission reopened")
    return target


def next_phase(kind: Literal["fleet", "unit"], phase: str, direction: Direction) -> str | None:
    """The one phase after `phase`, or None when the kind has no such step."""
    if phase == "restoring":
        return "complete"
    if kind == "fleet":
        order = _FLEET_CANDIDATE if direction == "candidate" else _FLEET_PREVIOUS
    else:
        order = _UNIT_CANDIDATE if direction == "candidate" else _UNIT_PREVIOUS
    if phase not in order or phase == "complete":
        return None
    return order[order.index(phase) + 1]


def _digest(record: Record) -> str:
    encoded = json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class Instruction(Record):
    """What the coordinator tells one unit to do; units act on nothing else.

    A unit refuses an instruction for another operation or unit, and answers
    each by its digest. `generation` names the write generation a `start`
    runs on (a `restore` runs on the unit's unchanged one); `image` is the selector of the
    direction's image on that unit; `deadline` is the coordinator's barrier
    bound for the answer; a `complete` names the operation's outcome.
    """

    operation: UUID
    unit: UnitKey
    sequence: int = Field(ge=1)
    action: InstructionAction
    direction: Direction
    image: tuple[Digest, Digest]
    maintenance_at: AwareDatetime
    generation: int | None = Field(default=None, ge=0)
    deadline: AwareDatetime | None = None
    outcome: Outcome | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.outcome is not None) != (self.action == "complete"):
            raise ValueError("a complete instruction names its outcome, and only it does")
        if (self.generation is not None) != (self.action == "start"):
            raise ValueError("a start instruction names its generation, and only it does")
        return self

    def same_order(self, other: Instruction) -> bool:
        """The same order ignoring its sequence and deadline: a continuation reissues nothing."""
        mask = {"sequence", "deadline"}
        return self.model_dump(exclude=mask) == other.model_dump(exclude=mask)

    @property
    def digest(self) -> str:
        return _digest(self)

    def answered_by(self, state: ReportState) -> bool:
        return state in _ANSWERS[self.action]


class Report(Record):
    """A unit's answer to exactly one instruction, named by its digest."""

    operation: UUID
    unit: UnitKey
    instruction: Digest
    state: ReportState
    at: AwareDatetime
    cohort: UnitCohort | None = None
    detail: str | None = Field(default=None, max_length=2048)
    evidence: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.cohort is not None) != (self.state == "drained"):
            raise ValueError("a drained report carries the unit's cohort, and only it does")
        if self.cohort is not None and self.cohort.unit != self.unit:
            raise ValueError("a report's cohort belongs to its own unit")
        if (self.detail is not None) != (self.state == "failed"):
            raise ValueError("a failed report explains itself, and only it does")
        if len(json.dumps(self.evidence)) > _MAX_EVIDENCE_BYTES:
            raise ValueError("report evidence exceeds its bound")
        return self

    @property
    def digest(self) -> str:
        return _digest(self)


Inclusion = Literal["included", "excluded", "failed", "unknown"]
# Once a unit leaves the operation it never returns to it; only a converge
# brings it back. A failed or unknown unit may still be excluded by the operator.
_INCLUSION_NEXT: dict[Inclusion, frozenset[Inclusion]] = {
    "included": frozenset({"included", "excluded", "failed", "unknown"}),
    "unknown": frozenset({"unknown", "failed", "excluded"}),
    "failed": frozenset({"failed", "excluded"}),
    "excluded": frozenset({"excluded"}),
}


class UnitStatus(Record):
    """The fleet journal's view of one remote unit."""

    unit: UnitKey
    inclusion: Inclusion = "included"
    reason: str | None = Field(default=None, max_length=512)
    instruction: Instruction | None = None
    report: Report | None = None
    start_attempts: int = Field(default=0, ge=0, le=2)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.reason is None) != (self.inclusion == "included"):
            raise ValueError("a unit that left the operation records why, and only it does")
        if self.instruction is not None and self.instruction.unit != self.unit:
            raise ValueError("an instruction belongs to its own unit")
        if self.report is not None and (
            self.report.unit != self.unit
            or self.instruction is None
            or self.report.instruction != self.instruction.digest
        ):
            raise ValueError("a recorded report answers the unit's current instruction")
        return self

    @property
    def answered(self) -> ReportState | None:
        """The state the current instruction was answered with, if it was."""
        if self.report is None or self.instruction is None:
            return None
        if self.report.state == "failed" or self.instruction.answered_by(self.report.state):
            return self.report.state
        return None


class AlertRecord(Record):
    """An alert journaled at first emission, then each delivery that landed."""

    alert: FleetAlert
    delivered: tuple[Literal["alert_row", "webhook", "agent"], ...] = ()

    def undelivered(self, route: AlertRoute) -> tuple[str, ...]:
        """The routes this alert has not reached yet."""
        return tuple(
            delivery.kind
            for delivery in deliveries(self.alert, route)
            if delivery.kind not in self.delivered
        )


class Decision(Record):
    kind: Literal["abort", "recover"]
    phase: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=2048)
    at: AwareDatetime


def _prefix[T](before: tuple[T, ...], after: tuple[T, ...], what: str) -> None:
    if after[: len(before)] != before:
        raise ValueError(f"journaled {what} are append-only")


def _set_once[T](before: T | None, after: T | None, what: str) -> None:
    if before is not None and after != before:
        raise ValueError(f"a journaled {what} never changes")


def _one_decision(decisions: tuple[Decision, ...]) -> None:
    if len(decisions) > 1:
        raise ValueError("an operation decides at most one abort or one recovery")


class FleetProgress(Record):
    """The coordinator's durable view of the whole operation."""

    maintenance_at: AwareDatetime
    # Write generation n, recorded read-only at `prepared`: an abort restores on it.
    admitted: GenerationRef | None = None
    units: tuple[UnitStatus, ...] = ()
    cohort: Cohort | None = None
    started_at: AwareDatetime | None = None
    resumed_at: AwareDatetime | None = None
    # The watch window's first sighting of each agent's runtime error or
    # quarantine (`workload.first_sightings`): a later sample no longer shows
    # it, but the window never forgets it.
    window_facts: tuple[AgentReport, ...] = ()
    verdicts: tuple[Verdict, ...] = ()
    alerts: tuple[AlertRecord, ...] = ()
    decisions: tuple[Decision, ...] = ()
    outcome: Outcome | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        keys = [status.unit.order for status in self.units]
        if keys != sorted(set(keys)):
            raise ValueError("fleet units are sorted and unique")
        alert_keys = [record.alert.key for record in self.alerts]
        if len(alert_keys) != len(set(alert_keys)):
            raise ValueError("an alert is journaled once per key")
        _one_decision(self.decisions)
        return self

    def decided(self, decision: Decision, maintenance_at: AwareDatetime | None) -> FleetProgress:
        """Record the decision; a recovery's own start and resume are judged afresh."""
        update: dict[str, object] = {"decisions": (*self.decisions, decision)}
        if decision.kind == "recover":
            update |= {"started_at": None, "resumed_at": None}
        if maintenance_at is not None:
            update["maintenance_at"] = maintenance_at
        return self.model_copy(update=update)

    def status(self, unit: UnitKey) -> UnitStatus:
        for status in self.units:
            if status.unit == unit:
                return status
        raise KeyError(unit.label)

    def alert(self, key: str) -> AlertRecord | None:
        return next((record for record in self.alerts if record.alert.key == key), None)

    def require_successor(self, after: FleetProgress) -> None:
        """What one journal write may change; a recovery alone moves the hold."""
        if after.maintenance_at != self.maintenance_at and len(after.decisions) == len(
            self.decisions
        ):
            raise ValueError("only a recovery decision takes a new maintenance hold")
        _set_once(self.admitted, after.admitted, "admitted generation")
        _set_once(self.cohort, after.cohort, "cohort")
        _set_once(self.outcome, after.outcome, "outcome")
        _prefix(self.window_facts, after.window_facts, "window facts")
        _prefix(self.verdicts, after.verdicts, "verdicts")
        _prefix(self.decisions, after.decisions, "decisions")
        if [r.alert for r in after.alerts[: len(self.alerts)]] != [r.alert for r in self.alerts]:
            raise ValueError("journaled alerts are append-only")
        for before, record in zip(self.alerts, after.alerts, strict=False):
            if not set(before.delivered) <= set(record.delivered):
                raise ValueError("a landed delivery is never forgotten")
        if [s.unit for s in after.units] != [s.unit for s in self.units]:
            raise ValueError("the operation's units never change")
        for before, status in zip(self.units, after.units, strict=True):
            _require_unit_successor(before, status)


def _require_unit_successor(before: UnitStatus, after: UnitStatus) -> None:
    if after.inclusion not in _INCLUSION_NEXT[before.inclusion]:
        raise ValueError(f"unit {before.unit.label} cannot return to the operation")
    if after.start_attempts < before.start_attempts:
        raise ValueError("start attempts only grow")
    if before.instruction is not None and (
        after.instruction is None or after.instruction.sequence < before.instruction.sequence
    ):
        raise ValueError(f"unit {before.unit.label}'s instructions only advance")


class UnitProgress(Record):
    """A remote unit's durable view: the instructions it acted on, by digest."""

    maintenance_at: AwareDatetime
    # The installed capability's generation at `prepared`; an abort restores on it.
    admitted: int | None = Field(default=None, ge=0)
    instruction: Instruction | None = None
    acted: tuple[Digest, ...] = ()
    report: Report | None = None
    decisions: tuple[Decision, ...] = ()
    outcome: Outcome | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        _one_decision(self.decisions)
        if self.instruction is not None and (
            not self.acted or self.acted[-1] != self.instruction.digest
        ):
            raise ValueError("the current instruction is the last one acted on")
        if self.report is not None and (
            self.instruction is None or self.report.instruction != self.instruction.digest
        ):
            raise ValueError("a unit reports only on its current instruction")
        return self

    def decided(self, decision: Decision, maintenance_at: AwareDatetime | None) -> UnitProgress:
        update: dict[str, object] = {"decisions": (*self.decisions, decision)}
        if maintenance_at is not None:
            update["maintenance_at"] = maintenance_at
        return self.model_copy(update=update)

    def require_successor(self, after: UnitProgress) -> None:
        if after.maintenance_at != self.maintenance_at and len(after.decisions) == len(
            self.decisions
        ):
            raise ValueError("only a recovery decision takes a new maintenance hold")
        _prefix(self.decisions, after.decisions, "decisions")
        _set_once(self.admitted, after.admitted, "admitted generation")
        _set_once(self.outcome, after.outcome, "outcome")
        _prefix(self.acted, after.acted, "instructions")
        if (
            self.instruction is not None
            and after.instruction is not None
            and after.instruction.sequence < self.instruction.sequence
        ):
            raise ValueError("a unit never acts on an older instruction")


def initial_progress(request: FleetRequest | UnitRequest) -> dict[str, Record]:
    """A new operation's progress: every listed unit included or excluded as captured."""
    if isinstance(request, UnitRequest):
        return {"unit": UnitProgress(maintenance_at=request.created_at)}
    statuses = [UnitStatus(unit=spec.unit) for spec in request.units] + [
        UnitStatus(unit=entry.unit, inclusion="excluded", reason=f"{entry.reason}: {entry.detail}")
        for entry in request.excluded
    ]
    ordered = tuple(sorted(statuses, key=lambda status: status.unit.order))
    return {"fleet": FleetProgress(maintenance_at=request.created_at, units=ordered)}
