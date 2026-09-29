"""Database write authority across a release: fence the old generation, admit the new.

Every upgrade and every rollback runs on a fresh write generation
(decisions/2026-09-26-internal-data-plane-always-authenticated.md). After the
old root stopped, `fencing` revokes the active generation and proves its
sessions closed; after the selector moved, `authorizing` mints the next
generation for the selected image and admits it before `starting`. A failed
candidate is fenced the same way before its predecessor gets a new number.

The home's ledger is the authority; the journal records intent before each
ledger transition and the non-secret receipt after it
(`authority_evidence.py`), both under the home operation lock. The data-plane
effects are `cli.commands.data_plane.write_generation`'s. A retry continues the
recorded generation or holds; it never mints a second pair or skips a fence.

The finite executor itself dials the home's owner-only socket as the OS-user
administrator acting as the gateway group (`adopt_executor_authority`): it is
the fencing authority, so no fence closes it.
"""

from __future__ import annotations

import getpass
from pathlib import Path
from urllib.parse import quote

from base.cluster.authority import (
    AuthorityRefusedError,
    Ledger,
    OperationAuthority,
    active_generation,
    require_ledger,
)
from base.deploy.release.runtime_release import current_pointer
from cli.release_transition.authority_evidence import Fence, FenceEvidence, GenerationRef, Issue
from cli.release_transition.journal import Journal, Operation
from cli.release_transition.request import ReleaseRef


def _authority(operation: Operation) -> OperationAuthority:
    if operation.direction is None:
        raise ValueError("write generations belong to release operations only")
    return OperationAuthority(operation=operation.request.id, direction=operation.direction)


def _home(operation: Operation) -> Path:
    return Path(operation.request.home)


def adopt_executor_authority(home: Path) -> None:
    """Dial the home's owner-only socket as the administrator acting as the
    ledger's gateway group, for this executor process only.

    `peer` authenticates the OS user, so no credential is involved; the startup
    role gives every statement exactly the gateway group's privileges while
    the session user stays the administrator, whom no fence census includes.
    """
    from base.cluster import db_identity, get_record, record_postgres_port
    from base.db.connections import adopt_administrator
    from base.db.pg_admin import pg_socket_dir

    ledger = require_ledger(home)
    record = get_record(home)
    if record is None:
        raise RuntimeError(f"no cluster record for home {home}; cannot dial its Postgres")
    query = "&".join(
        (
            f"host={quote(str(pg_socket_dir(home=home)), safe='')}",
            f"port={record_postgres_port(record)}",
            f"options={quote(f'-c role={ledger.groups.gateway}', safe='')}",
        )
    )
    user, database = quote(getpass.getuser(), safe=""), quote(db_identity(), safe="")
    adopt_administrator(f"postgresql://{user}@/{database}?{query}")


def preflight() -> GenerationRef:
    """Read-only: the home holds exactly one admitted generation to fence; it is
    also the generation an abort restores on."""
    from cli.commands.data_plane.write_generation import preflight_write_authority

    return GenerationRef.of(preflight_write_authority())


def _unfinished(ledger: Ledger) -> list[int]:
    return [entry.number for entry in ledger.revoked if entry.state != "closed"]


def _fence_target(operation: Operation) -> GenerationRef:
    """The generation this direction revokes: the ledger's admitted one, which a
    recovery requires to be exactly the candidate's issue."""
    ledger = require_ledger(_home(operation))
    if ledger.active is None or ledger.pending is not None or _unfinished(ledger):
        raise AuthorityRefusedError(
            "fencing requires exactly one admitted write generation and no unfinished one"
        )
    target = GenerationRef.of(ledger.active)
    if operation.direction == "previous":
        issued = operation.issue("candidate")
        if issued is None or issued.generation != target:
            raise AuthorityRefusedError(
                "a recovery fences exactly the candidate's admitted generation"
            )
    return target


def _closed_entry(operation: Operation, generation: GenerationRef) -> tuple[bool, str | None]:
    """The ledger's closed record of `generation`: (dropped, drop error)."""
    ledger = require_ledger(_home(operation))
    entry = next((item for item in ledger.revoked if item.number == generation.number), None)
    if entry is None or entry.state != "closed" or entry.roles != generation.roles:
        raise AuthorityRefusedError(
            f"the ledger does not record write generation {generation.number} closed"
        )
    return entry.dropped, entry.drop_error


def _require_fenceable(operation: Operation, record: Fence) -> None:
    """Before any revocation effect: the ledger's unrevoked generation is the
    recorded one, or the recorded one is already revoked."""
    ledger = require_ledger(_home(operation))
    current = ledger.unrevoked
    if current is None:
        if not any(entry.number == record.generation.number for entry in ledger.revoked):
            raise AuthorityRefusedError(
                f"write generation {record.generation.number} is neither admitted nor revoked"
            )
    elif GenerationRef.of(current) != record.generation:
        raise AuthorityRefusedError(
            f"write generation {current.number} is not the recorded fence target "
            f"{record.generation.number}"
        )


def fence(journal: Journal) -> None:
    """Revoke this direction's generation and prove every writer of it closed.

    Intent (`Fence` revoking, naming the generation) precedes the ledger's
    `revoking`; the closure receipt follows the ledger's `closed`. A closed
    receipt is final: a retry only checks the ledger agrees.
    """
    from cli.commands.data_plane.write_generation import fence_write_generation

    operation = journal.operation
    authority = _authority(operation)
    record = operation.fence(authority.direction)
    if record is None:
        record = Fence(
            direction=authority.direction, generation=_fence_target(operation), state="revoking"
        )
        journal.record_fence(record)
    if record.state == "closed":
        _closed_entry(operation, record.generation)
        return
    _require_fenceable(operation, record)
    outcome = fence_write_generation(authority)
    dropped, drop_error = _closed_entry(operation, record.generation)
    evidence = FenceEvidence(
        roles=outcome.closure.roles,
        pooler=outcome.pooler,
        terminated=outcome.closure.terminated,
        rounds=outcome.closure.rounds,
        dropped=dropped,
        drop_error=drop_error,
    )
    journal.record_fence(
        Fence(
            direction=record.direction,
            generation=record.generation,
            state="closed",
            evidence=evidence,
        )
    )


def _require_issuable(operation: Operation, record: Issue) -> None:
    """Before any mint effect: the selected target and the recorded number are
    exactly what the ledger will allocate or already holds for this authority."""
    home = _home(operation)
    if current_pointer(home / "releases") != record.selector:
        raise AuthorityRefusedError("authorizing requires the selected target release")
    ledger = require_ledger(home)
    current = ledger.unrevoked
    if _unfinished(ledger):
        raise AuthorityRefusedError("a revoked write generation is not proven closed")
    if current is None:
        if ledger.next_number != record.number:
            raise AuthorityRefusedError(
                f"the ledger allocates {ledger.next_number}, not the recorded {record.number}"
            )
        return
    origin = current.origin
    if (current.number, origin.kind, origin.operation, origin.direction) != (
        record.number,
        "operation",
        str(operation.request.id),
        record.direction,
    ):
        raise AuthorityRefusedError(
            f"write generation {current.number} is not this operation's recorded issue"
        )


def authorize(journal: Journal, target: ReleaseRef) -> None:
    """Mint the next generation for the selected `target` and admit it.

    Intent (`Issue` minting, naming the number) precedes the secret and the
    ledger's `pending`; the authorized receipt follows the ledger's `active`.
    A retry reconciles exactly the recorded number and never allocates another.
    """
    from cli.commands.data_plane.write_generation import admit_write_generation

    operation = journal.operation
    authority = _authority(operation)
    record = operation.issue(authority.direction)
    if record is None:
        record = Issue(
            direction=authority.direction,
            number=require_ledger(_home(operation)).next_number,
            selector=target.selector,
            state="minting",
        )
        _require_issuable(operation, record)
        journal.record_issue(record)
    elif record.state == "authorized":
        _require_active(operation, record)
        return
    else:
        _require_issuable(operation, record)
    generation = GenerationRef.of(admit_write_generation(authority))
    if generation.number != record.number:
        raise AuthorityRefusedError(
            f"admitted write generation {generation.number} is not the recorded {record.number}"
        )
    journal.record_issue(
        Issue(
            direction=record.direction,
            number=record.number,
            selector=record.selector,
            state="authorized",
            generation=generation,
        )
    )


def _issued(operation: Operation) -> Issue:
    record = None if operation.direction is None else operation.issue(operation.direction)
    if record is None or record.generation is None:
        raise RuntimeError("the release has no authorized write generation for its direction")
    return record


def _require_active(operation: Operation, record: Issue) -> None:
    active = GenerationRef.of(active_generation(_home(operation)))
    if active != record.generation:
        raise AuthorityRefusedError(
            f"the home's active write generation {active.number} is not the operation's "
            f"issued generation {record.number}"
        )


def _unit_generation(operation: Operation) -> None:
    """A remote unit starts only on a generation it holds: an abort's unchanged one.

    A new generation reaches a unit through its capability exchange over the
    coordinator channel (slice dbgen-8); until then a unit start refuses.
    """
    from base.cluster.authority.unit import load_unit_capability

    unit = operation.unit
    assert unit is not None  # noqa: S101 — called for unit operations only
    installed = load_unit_capability(_home(operation))
    if operation.phase != "restoring":
        raise AuthorityRefusedError(
            "a unit starts on a new write generation only after its capability exchange "
            "over the coordinator channel (slice dbgen-8)"
        )
    if installed is None or installed.generation.number != unit.admitted:
        raise AuthorityRefusedError("an aborted unit restores only on its unchanged generation")


def _admitted(operation: Operation) -> GenerationRef:
    """An abort restores on generation n, recorded at `prepared` and never fenced."""
    fleet = operation.fleet
    if fleet is None or fleet.admitted is None or operation.db_fences:
        raise AuthorityRefusedError("an abort restores only on its recorded unfenced generation")
    return fleet.admitted


def require_issued(operation: Operation) -> None:
    """Before root start: the ledger's active generation is exactly this
    direction's authorized issue (an abort's: the unchanged admitted one), so
    the launch delivers (and binds into its digest) only that generation. PITR
    reuses the active generation."""
    if operation.pitr is not None:
        return
    if operation.unit is not None:
        _unit_generation(operation)
        return
    if operation.phase == "restoring":
        admitted = _admitted(operation)
        if GenerationRef.of(active_generation(_home(operation))) != admitted:
            raise AuthorityRefusedError("the write generation changed during an aborted release")
        return
    _require_active(operation, _issued(operation))


def verify_active(operation: Operation) -> None:
    """After readiness: exactly the issued generation writes and nothing older can."""
    from cli.commands.data_plane.write_generation import verify_write_generation

    if operation.unit is not None:
        _unit_generation(operation)
        return
    if operation.phase == "restoring":
        generation: GenerationRef | None = _admitted(operation)
    else:
        generation = _issued(operation).generation
    if generation is None:
        raise RuntimeError("an authorized issue names its generation")
    verify_write_generation(generation.number, generation.credential_digest)
