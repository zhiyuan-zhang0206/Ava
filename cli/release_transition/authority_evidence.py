"""Write-generation records of a release journal: one fence and one issue per direction.

The home's ledger (`shared.cluster.authority`) is the authority; these records
are the operation's intent and its non-secret receipts, ordered around the
ledger's own transitions. A fence names the generation it revokes before any
revocation, and records the closure census once closed. An issue names the
number it will mint before the secret exists, and the full generation once it
is active. Only a generation's number, login names and credential digest ever
reach the journal.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Self

from pydantic import Field, model_validator

from cli.release_transition.request import Digest, Record
from shared.cluster.authority.model import Direction, Generation, RoleName

_DIRECTIONS: tuple[Direction, ...] = ("candidate", "previous")
# Release phases, in order, at which a direction's closed fence (and, from
# `starting`, its authorized issue) must already exist.
_FENCED = frozenset(
    {
        "selecting",
        "authorizing",
        "starting",
        "observing",
        "starting_units",
        "resuming",
        "watching",
        "complete",
    }
)
_ISSUED = frozenset({"starting", "observing", "starting_units", "resuming", "watching", "complete"})
# An abort (`restoring`) never fenced: it restores on the unchanged generation.
_UNFENCED = frozenset({"prepared", "dispatching", "quiescing", "stopping", "restoring"})


class GenerationRef(Record):
    """A write generation named without its credentials."""

    number: int = Field(ge=0)
    gateway: RoleName
    runner: RoleName
    credential_digest: Digest

    @classmethod
    def of(cls, generation: Generation) -> GenerationRef:
        return cls(
            number=generation.number,
            gateway=generation.gateway,
            runner=generation.runner,
            credential_digest=generation.credential_digest,
        )

    @property
    def roles(self) -> tuple[str, str]:
        return self.gateway, self.runner


class FenceEvidence(Record):
    """The closure receipt: an empty census after termination, never a sent signal."""

    roles: tuple[RoleName, ...]
    pooler: Literal["not-running", "stopped", "forced"]
    terminated: int = Field(ge=0)
    rounds: int = Field(ge=0)
    remaining_sessions: Literal[0] = 0
    prepared_transactions: Literal[0] = 0
    # The fenced generation's prune outcome, as the ledger records it.
    dropped: bool
    drop_error: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def coherent_drop(self) -> Self:
        if self.dropped and self.drop_error is not None:
            raise ValueError("a dropped generation leaves no drop error")
        return self


class Fence(Record):
    direction: Direction
    generation: GenerationRef
    state: Literal["revoking", "closed"]
    evidence: FenceEvidence | None = None

    @model_validator(mode="after")
    def coherent_state(self) -> Self:
        if (self.state == "closed") != (self.evidence is not None):
            raise ValueError("a fence carries closure evidence exactly when it is closed")
        if self.evidence is not None and not set(self.generation.roles) <= set(self.evidence.roles):
            raise ValueError("closure evidence must cover the fenced generation's logins")
        return self


class Issue(Record):
    direction: Direction
    number: int = Field(ge=0)
    selector: tuple[Digest, Digest]
    state: Literal["minting", "authorized"]
    generation: GenerationRef | None = None

    @model_validator(mode="after")
    def coherent_state(self) -> Self:
        if (self.state == "authorized") != (self.generation is not None):
            raise ValueError("an issue names its full generation exactly when it is authorized")
        if self.generation is not None and self.generation.number != self.number:
            raise ValueError("an authorized issue is the number its intent recorded")
        return self


def _one_per_direction(records: Sequence[Fence] | Sequence[Issue], kind: str) -> None:
    directions = [record.direction for record in records]
    if directions != list(_DIRECTIONS[: len(directions)]):
        raise ValueError(f"{kind} records are one per direction, candidate before previous")


def _of[R: (Fence, Issue)](records: Sequence[R], direction: Direction) -> R | None:
    return next((record for record in records if record.direction == direction), None)


def _require_direction(
    phase: str, fence: Fence | None, issue: Issue | None, direction: Direction
) -> None:
    if phase in _UNFENCED and (fence is not None or issue is not None):
        raise ValueError(f"{direction} write authority is journaled only from fencing")
    if phase in {"fencing", "selecting"} and issue is not None:
        raise ValueError("an issue is journaled only while its direction authorizes")
    if phase in _FENCED and (fence is None or fence.state != "closed"):
        raise ValueError(f"{phase} requires the {direction} direction's closed fence")
    if phase in _ISSUED and (issue is None or issue.state != "authorized"):
        raise ValueError(f"{phase} requires the {direction} direction's authorized issue")


def _require_numbering(fences: Sequence[Fence], issues: Sequence[Issue]) -> None:
    """Issue numbers strictly increase, each above the generation its fence revoked."""
    numbers = [issue.number for issue in issues]
    if numbers != sorted(set(numbers)):
        raise ValueError("issued generation numbers strictly increase")
    for fence in fences:
        issue = _of(issues, fence.direction)
        if issue is not None and issue.number <= fence.generation.number:
            raise ValueError("an issued generation is newer than the one its fence revoked")


def _require_recovery(fences: Sequence[Fence], issues: Sequence[Issue]) -> None:
    """A recovery follows a candidate that was fenced and admitted, and fences
    exactly the candidate's admitted generation."""
    _require_direction("complete", _of(fences, "candidate"), _of(issues, "candidate"), "candidate")
    candidate = _of(issues, "candidate")
    recovery = _of(fences, "previous")
    if (
        candidate is not None
        and recovery is not None
        and recovery.generation != candidate.generation
    ):
        raise ValueError("a recovery fences exactly the candidate's issued generation")


def require_coherent(
    phase: str,
    direction: Direction | None,
    fences: Sequence[Fence],
    issues: Sequence[Issue],
) -> None:
    """The records a journal state requires, and nothing it cannot have yet.

    An operation without a fencing direction carries none: PITR and an
    aborted release reuse the active generation, a remote unit receives it.
    A recovery (direction `previous`) first fences the candidate's
    authorized generation; issue numbers strictly increase, so a return to the
    previous image is always a new generation.
    """
    if direction is None:
        if fences or issues:
            raise ValueError("this operation reuses the active write generation; it never fences")
        return
    _one_per_direction(fences, "fence")
    _one_per_direction(issues, "issue")
    if direction == "candidate" and (len(fences) > 1 or len(issues) > 1):
        raise ValueError("a candidate operation has no recovery write authority")
    _require_numbering(fences, issues)
    _require_direction(phase, _of(fences, direction), _of(issues, direction), direction)
    if direction == "previous":
        _require_recovery(fences, issues)
