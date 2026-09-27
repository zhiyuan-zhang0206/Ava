"""Captured fleet release policy and the frozen eligible cohort.

Pure: no clock, file, database or network access. Every time is an input.
The fleet coordinator captures `FleetPolicy` in its request and threads it to
each phase; it captures the `Cohort` once, at `quiescing`, from the
drain reports of the included units.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from cli.release_transition.request import Record
from shared.maintenance_state import MaintenanceHold


def _normalized_absolute(value: str) -> bool:
    """The unit records its own home (`machine_units.home`), in its own
    platform's form; a Windows runner's is `C:\\...`."""
    flavours: tuple[PurePath, ...] = (PurePosixPath(value), PureWindowsPath(value))
    return any(
        path.is_absolute() and str(path) == value and ".." not in path.parts for path in flavours
    )


class UnitKey(Record):
    """One unit: an install under its own home on one machine (`M:HOME`)."""

    machine: str = Field(min_length=1, max_length=128)
    home: str = Field(min_length=1, max_length=4096)

    @field_validator("machine", "home")
    @classmethod
    def printable(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("unit identities cannot contain control characters")
        return value

    @field_validator("home")
    @classmethod
    def absolute_home(cls, value: str) -> str:
        if not _normalized_absolute(value):
            raise ValueError("unit homes must be normalized absolute paths")
        return value

    @property
    def label(self) -> str:
        return f"{self.machine}:{self.home}"

    @property
    def order(self) -> tuple[str, str]:
        return self.machine, self.home


class AlertRoute(Record):
    """Where fleet alerts go besides the cluster's own alerts table.

    `recipient_agent` only observes; the coordinator stays the recovery
    authority. `webhook_file` names an owner-only file under the
    coordinator's `$AVA_HOME/secrets/` holding the out-of-band webhook URL, so
    the URL (usually a bearer in itself) never enters a request or journal.
    """

    recipient_agent: int | None = Field(default=None, ge=1)
    webhook_file: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class FleetPolicy(Record):
    """The request's `policy` block, captured once and never re-read from Settings.

    Bounds are whole seconds so every boundary is exact. `close_s` and
    `cancel_grace_s` are the writer-closure bounds the one-home effects
    (`cli/release_transition/local.py`) apply at every unit's `closing`.
    The threshold is an integer percent so "exactly at the threshold" is exact.
    """

    drain_s: int = Field(default=90, ge=1)
    close_s: int = Field(default=30, ge=1)
    cancel_grace_s: int = Field(default=10, ge=1)
    start_s: int = Field(default=660, ge=1)
    watch_s: int = Field(default=1800, ge=1)
    threshold_percent: int = Field(default=20, ge=0, le=100)
    min_affected: int = Field(default=2, ge=1)
    alert_route: AlertRoute = Field(default_factory=AlertRoute)
    # Oscillation guard: a candidate a previous operation rejected needs the
    # exact rejecting operation named here (publication.require_admissible).
    acknowledged_rejection: UUID | None = None

    def exceeds(self, affected: int, cohort: int) -> bool:
        """More than `threshold_percent` of the cohort, and at least `min_affected`."""
        if not 0 <= affected <= cohort:
            raise ValueError("affected agents must be a subset of the cohort")
        return affected >= self.min_affected and affected * 100 > self.threshold_percent * cohort


def _agent_ids(values: tuple[int, ...], what: str) -> tuple[int, ...]:
    if any(agent < 1 for agent in values):
        raise ValueError(f"{what} must be positive agent ids")
    if list(values) != sorted(set(values)):
        raise ValueError(f"{what} must be sorted and unique")
    return values


class UnitCohort(Record):
    """One included unit's frozen restart cohort and the drain's cancellations."""

    unit: UnitKey
    agents: tuple[int, ...] = ()
    reaped: tuple[int, ...] = ()

    @model_validator(mode="after")
    def canonical(self) -> Self:
        _agent_ids(self.agents, "cohort agents")
        _agent_ids(self.reaped, "reaped agents")
        if not set(self.reaped) <= set(self.agents):
            raise ValueError("reaped agents must belong to the unit's cohort")
        return self


class Cohort(Record):
    """Agents whose runtime was live on an included unit when quiescing began.

    Every included unit appears exactly once, the gateway unit among them, even
    with no agents; units excluded before dispatch never appear. Idle agents
    (the maintenance hold's `parked` set) are not members.
    """

    captured_at: AwareDatetime
    gateway: UnitKey
    units: tuple[UnitCohort, ...]

    @model_validator(mode="after")
    def complete(self) -> Self:
        keys = [entry.unit.order for entry in self.units]
        if keys != sorted(set(keys)):
            raise ValueError("cohort units must be sorted and unique")
        if self.gateway.order not in keys:
            raise ValueError("the gateway unit must be an included cohort unit")
        members = [agent for entry in self.units for agent in entry.agents]
        if len(members) != len(set(members)):
            raise ValueError("an agent belongs to exactly one cohort unit")
        return self

    @property
    def size(self) -> int:
        return sum(len(entry.agents) for entry in self.units)

    @property
    def members(self) -> dict[int, UnitKey]:
        return {agent: entry.unit for entry in self.units for agent in entry.agents}

    @property
    def unit_keys(self) -> tuple[UnitKey, ...]:
        return tuple(entry.unit for entry in self.units)

    def agents_on(self, unit: UnitKey) -> tuple[int, ...]:
        for entry in self.units:
            if entry.unit == unit:
                return entry.agents
        raise KeyError(unit.label)


def drain_report(unit: UnitKey, hold: MaintenanceHold) -> UnitCohort:
    """A unit's cohort outcome from its drained maintenance hold.

    The hold's restart commands name the agents whose runtime was live; the
    reaped receipts name the turns the bounded drain cancelled. A hold that has
    not drained, or carries unsettled failures, has no cohort outcome yet.
    """
    if hold.phase in {"preparing", "draining"}:
        raise ValueError(f"unit {unit.label} has not finished draining")
    if unsettled := hold.unsettled_failures():
        raise ValueError(f"unit {unit.label} has unsettled drain failures: {sorted(unsettled)}")
    return UnitCohort(
        unit=unit, agents=tuple(sorted(hold.commands)), reaped=tuple(sorted(hold.reaped))
    )


def capture_cohort(
    *, gateway: UnitKey, reports: Sequence[UnitCohort], captured_at: datetime
) -> Cohort:
    """Freeze the eligible cohort from every included unit's drain report."""
    ordered = tuple(sorted(reports, key=lambda entry: entry.unit.order))
    return Cohort(captured_at=captured_at, gateway=gateway, units=ordered)
