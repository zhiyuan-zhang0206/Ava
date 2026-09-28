"""Known-good publication: the cluster release state a complete operation writes.

Pure: `publish` returns the next `FleetState` (or nothing to write) and the
coordinator writes it to `releases/fleet-state.json` only when the operation
is complete. A degraded release never becomes last-known-good, and neither
does a release no live workload exercised. Rejections are append-only
history; a rejected candidate is admitted again only when a new request names
the exact operation that rejected it.
"""

from __future__ import annotations

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, model_validator

from cli.release_fleet.policy import UnitKey
from cli.release_fleet.workload import Verdict
from cli.release_transition.request import Commit, Digest, Record

Completed = Literal["clean", "degraded", "aborted", "recovered"]


class FleetRelease(Record):
    """What every unit's candidate must agree on; artifacts differ per host."""

    source_commit: Commit
    schema_digest: Digest
    sql_inventory_digest: Digest


class Rejection(Record):
    release: FleetRelease
    operation: UUID
    at: AwareDatetime


def _sorted_units(units: tuple[UnitKey, ...]) -> tuple[UnitKey, ...]:
    keys = [unit.order for unit in units]
    if keys != sorted(set(keys)):
        raise ValueError("units must be sorted and unique")
    return units


class FleetState(Record):
    """`releases/fleet-state.json`: current release, stale units, known-good, rejections.

    A stale unit (excluded, failed or unknown) is not on `current` and rejoins
    only through a converge operation.
    """

    version: Literal[1] = 1
    operation: UUID
    updated_at: AwareDatetime
    current: FleetRelease
    stale_units: tuple[UnitKey, ...] = ()
    last_known_good: FleetRelease | None = None
    rejected: tuple[Rejection, ...] = ()

    @model_validator(mode="after")
    def canonical(self) -> Self:
        _sorted_units(self.stale_units)
        return self


class Completion(Record):
    """How one fleet operation completed, and what it leaves stale."""

    operation: UUID
    at: AwareDatetime
    outcome: Completed
    previous: FleetRelease
    candidate: FleetRelease
    stale_units: tuple[UnitKey, ...] = ()
    exercised: bool = False

    @model_validator(mode="after")
    def coherent(self) -> Self:
        _sorted_units(self.stale_units)
        if self.previous == self.candidate:
            raise ValueError("a fleet operation moves between two distinct releases")
        if self.exercised and self.outcome != "clean":
            raise ValueError("only a clean commit carries workload proof")
        return self

    @classmethod
    def committed(
        cls,
        verdict: Verdict,
        *,
        operation: UUID,
        previous: FleetRelease,
        candidate: FleetRelease,
        excluded: tuple[UnitKey, ...],
    ) -> Completion:
        """A commit verdict's completion: its failed and unknown units stay stale."""
        if verdict.action != "commit" or verdict.outcome is None:
            raise ValueError("only a commit verdict completes on the candidate")
        stale = {
            unit.order: unit for unit in (*excluded, *verdict.failed_units, *verdict.unknown_units)
        }
        return cls(
            operation=operation,
            at=verdict.decided_at,
            outcome=verdict.outcome,
            previous=previous,
            candidate=candidate,
            stale_units=tuple(stale[order] for order in sorted(stale)),
            exercised=verdict.outcome == "clean" and verdict.exercised,
        )


def publish(prior: FleetState | None, completion: Completion) -> FleetState | None:
    """The next cluster release state; None when the completion changes nothing.

    - `aborted`: nothing was fenced or selected; the state is unchanged.
    - `recovered`: back on previous; the candidate is appended as rejected.
    - `degraded`: on the candidate; last-known-good is unchanged.
    - `clean`: on the candidate; it becomes last-known-good only if exercised.
    """
    if prior is not None and prior.current != completion.previous:
        raise ValueError("the operation did not start from the published current release")
    if completion.outcome == "aborted":
        return None
    known_good = None if prior is None else prior.last_known_good
    rejected = () if prior is None else prior.rejected
    current = completion.candidate
    if completion.outcome == "recovered":
        current = completion.previous
        rejection = Rejection(
            release=completion.candidate, operation=completion.operation, at=completion.at
        )
        rejected = (*rejected, rejection)
    elif completion.outcome == "clean" and completion.exercised:
        known_good = completion.candidate
    return FleetState(
        operation=completion.operation,
        updated_at=completion.at,
        current=current,
        stale_units=completion.stale_units,
        last_known_good=known_good,
        rejected=rejected,
    )


def require_admissible(
    state: FleetState | None, candidate: FleetRelease, acknowledged: UUID | None
) -> None:
    """Oscillation guard: a rejected candidate needs its rejection acknowledged.

    The acknowledgement must name the latest operation that rejected this
    exact release; an acknowledgement that names nothing is refused too.
    """
    rejections = [] if state is None else [r for r in state.rejected if r.release == candidate]
    if not rejections:
        if acknowledged is not None:
            raise ValueError("the acknowledged rejection does not reject this candidate")
        return
    latest = rejections[-1].operation
    if acknowledged != latest:
        raise ValueError(
            f"candidate {candidate.source_commit} was rejected by operation {latest}; "
            "a new request must acknowledge that rejection"
        )
