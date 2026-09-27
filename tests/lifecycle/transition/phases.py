"""Advance a release journal with the write-generation receipts its phases require.

For tests of journal ordering, launchers and continuation, not of the database
fence itself (tests/lifecycle/transition/test_authority.py and
tests/lifecycle/db_authority/test_release_fence.py): leaving `fencing` records
the closed fence the real phase would, leaving `authorizing` the authorized
issue. The candidate direction fences generation 0 and issues 1; a recovery
fences the candidate's issue and issues the next number.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from cli.release_fleet.policy import UnitCohort, capture_cohort
from cli.release_fleet.progress import initial_progress
from cli.release_fleet.request import FleetRequest
from cli.release_transition.authority_evidence import Fence, FenceEvidence, GenerationRef, Issue
from cli.release_transition.journal import Journal, Operation, Phase

RELEASE_PHASES: tuple[Phase, ...] = (
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


def generation(number: int) -> GenerationRef:
    return GenerationRef(
        number=number,
        gateway=f"ava_g{number}_gateway",
        runner=f"ava_g{number}_runner",
        credential_digest=format(number + 1, "064x"),
    )


def closed(direction: str, fenced: GenerationRef) -> Fence:
    return Fence.model_validate(
        {
            "direction": direction,
            "generation": fenced,
            "state": "closed",
            "evidence": FenceEvidence(
                roles=fenced.roles, pooler="stopped", terminated=0, rounds=0, dropped=True
            ),
        }
    )


def authorized(direction: str, issued: GenerationRef, operation: Operation) -> Issue:
    request = operation.request
    assert isinstance(request, FleetRequest)
    target = request.candidate if direction == "candidate" else request.previous
    return Issue.model_validate(
        {
            "direction": direction,
            "number": issued.number,
            "selector": target.selector,
            "state": "authorized",
            "generation": issued,
        }
    )


def fenced_generation(operation: Operation) -> GenerationRef:
    """What a direction's fence revokes: generation 0, or the candidate's issue."""
    candidate = operation.issue("candidate")
    if operation.direction == "previous" and candidate is not None:
        assert candidate.generation is not None
        return candidate.generation
    return generation(0)


def _recovered_candidate(operation: Operation, number: int) -> tuple[list[Fence], list[Issue]]:
    """A recovery's candidate records: kept when present, else synthesized below `number`."""
    fences = [f for f in operation.db_fences if f.direction == "candidate"]
    issues = [i for i in operation.db_issues if i.direction == "candidate"]
    if not issues:
        fences = [closed("candidate", generation(number - 2))]
        issues = [authorized("candidate", generation(number - 1), operation)]
    return fences, issues


def records(
    operation: Operation, phase: Phase, issued: GenerationRef | None = None
) -> dict[str, object]:
    """Journal fields a synthetic operation at `phase` carries: a recovery also
    carries the failed candidate's closed fence and authorized issue. `issued`
    names the direction's authorized generation (default: 1, or 2 for a
    recovery); every earlier record is numbered below it."""
    direction = operation.direction
    assert direction is not None
    number = issued.number if issued is not None else 2 if direction == "previous" else 1
    fences: list[Fence] = []
    issues: list[Issue] = []
    fenced = generation(number - 1)
    if direction == "previous":
        fences, issues = _recovered_candidate(operation, number)
        assert issues[0].generation is not None
        fenced = issues[0].generation
    order = RELEASE_PHASES.index(phase)
    if order > RELEASE_PHASES.index("fencing"):
        fences.append(closed(direction, fenced))
    if order > RELEASE_PHASES.index("authorizing"):
        issues.append(authorized(direction, issued or generation(number), operation))
    return {"db_fences": tuple(fences), "db_issues": tuple(issues)}


def at_phase(phase: Phase, *, issued: GenerationRef | None = None, **fields: Any) -> Operation:
    """A validated operation with `fields` placed at `phase`, carrying the
    receipts that phase requires (`fields` alone may not be a valid journal
    state, as for a recovery before its candidate's records exist) and, for a
    fleet request without explicit progress, a fresh fleet progress (completed
    `clean` at `complete`)."""
    request = fields["request"]
    if isinstance(request, FleetRequest) and "fleet" not in fields:
        progress = initial_progress(request)["fleet"]
        if phase == "complete":
            progress = progress.model_copy(update={"outcome": "clean"})
        fields = fields | {"fleet": progress}
    draft = Operation.model_construct(**fields)
    return Operation.model_validate(
        draft.model_dump() | records(draft, phase, issued) | {"phase": phase}
    )


def seed_active(home: Path, number: int) -> GenerationRef:
    """A private ledger whose active generation is `number`, reached through the
    ledger's own transitions (birth, then one fence and mint per operation).

    Settings-free and database-free: for code that only compares the journal
    with the ledger. The catalog side is proven in tests/lifecycle/db_authority/.
    """
    from shared.cluster.authority import (
        GATEWAY_GROUP,
        RUNNER_GROUP,
        BirthAuthority,
        ClosureEvidence,
        Groups,
        OperationAuthority,
        VerifiedGeneration,
        activate,
        create_ledger,
    )
    from shared.cluster.authority.ledger import begin_mint, begin_revoke, mark_closed

    def encrypt(name: str, _password: str) -> str:
        return f"SCRAM-SHA-256$4096:c2VlZA==${name}"

    birth = BirthAuthority()
    groups = Groups(gateway=GATEWAY_GROUP, runner=RUNNER_GROUP)
    create_ledger(home, owner="ava", groups=groups, authority=birth)
    authority: BirthAuthority | OperationAuthority = birth
    active = None
    for _ in range(number + 1):
        if active is not None:
            authority = OperationAuthority(operation=uuid4(), direction="candidate")
            revoking = begin_revoke(home, authority)
            roles = tuple(name for entry in revoking for name in entry.roles)
            mark_closed(home, authority, ClosureEvidence(roles=roles, terminated=0, rounds=0))
        pending = begin_mint(home, authority, encrypt=encrypt)
        verified = VerifiedGeneration(pending.number, pending.credential_digest, pending.roles)
        active = activate(home, authority, verified)
    assert active is not None
    return GenerationRef.of(active)


def journal_fence(handle: Journal) -> Operation:
    """Close the fencing direction's fence: its intent (unless recorded), then its
    receipt. An already closed fence is kept."""
    operation = handle.operation
    direction = operation.direction
    assert direction is not None
    current = operation.fence(direction)
    if current is not None and current.state == "closed":
        return operation
    receipt = closed(direction, fenced_generation(operation))
    if current is None:
        handle.record_fence(receipt.model_copy(update={"state": "revoking", "evidence": None}))
    return handle.record_fence(receipt)


def journal_issue(handle: Journal) -> Operation:
    """Authorize the authorizing direction's issue: its intent (unless recorded),
    then its receipt. An already authorized issue is kept."""
    operation = handle.operation
    direction = operation.direction
    assert direction is not None
    current = operation.issue(direction)
    if current is not None and current.state == "authorized":
        return operation
    receipt = authorized(direction, generation(fenced_generation(operation).number + 1), operation)
    if current is None:
        handle.record_issue(receipt.model_copy(update={"state": "minting", "generation": None}))
    return handle.record_issue(receipt)


def step(handle: Journal, phase: Phase) -> Operation:
    """`handle.advance(phase)`, first journaling the receipt the phase it leaves
    requires; completing a fleet operation records a `clean` outcome (or
    `recovered` for a recovery)."""
    operation = handle.operation
    if operation.direction is not None and operation.phase == "fencing":
        journal_fence(handle)
    if operation.direction is not None and operation.phase == "authorizing":
        journal_issue(handle)
    if operation.fleet is not None:
        _stamp(handle, phase)
    if phase == "complete" and operation.fleet is not None:
        return handle.complete("clean" if operation.direction == "candidate" else "recovered")
    return handle.advance(phase)


def _stamp(handle: Journal, phase: Phase) -> None:
    """What the coordinator would have journaled before leaving the phase: the
    frozen cohort (the gateway unit, no agents), the start and resume stamps."""
    progress = handle.operation.fleet
    request = handle.operation.request
    assert progress is not None and isinstance(request, FleetRequest)
    changes: dict[str, object] = {}
    if phase == "stopping" and progress.cohort is None:
        changes["cohort"] = capture_cohort(
            gateway=request.gateway,
            reports=[UnitCohort(unit=request.gateway)],
            captured_at=request.created_at,
        )
    if phase == "observing" and progress.started_at is None:
        changes["started_at"] = request.created_at
    if phase == "watching" and progress.resumed_at is None:
        changes["resumed_at"] = request.created_at
    if changes:
        handle.record_fleet(progress.model_copy(update=changes))


def advance_to(handle: Journal, target: Phase) -> Operation:
    """Step through every release phase after the current one up to `target`
    (a recovery skips `watching`)."""
    current = handle.operation
    start = RELEASE_PHASES.index(current.phase) + 1
    for phase in RELEASE_PHASES[start : RELEASE_PHASES.index(target) + 1]:
        if phase == "watching" and current.direction == "previous":
            continue
        current = step(handle, phase)
    return current
