"""Current release publication and pending rollout share one evidence field.

Only the existing rollout's verified producers may supply closure/readback facts.
These storage contracts do not authenticate caller-created Python values, attest
unregistered credential holders, or activate caller protocol support by themselves.
"""

from __future__ import annotations

import json
from typing import Literal, Self
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, Field, model_validator

from shared.managed_writer_barrier import (
    Digest,
    EvidenceModel,
    ManagedUnit,
    ManagedWriterBarrierError,
    RolloutIdentity,
    lock_registered_units,
    lock_rollout,
)


class PublishedUnit(ManagedUnit):
    """inventory_digest is the FULL prepared receipt, including service roster."""

    artifact_digest: Digest
    manifest_digest: Digest


class CommittedPublication(EvidenceModel):
    publication_id: UUID
    operation: RolloutIdentity
    committed_at: AwareDatetime
    units: tuple[PublishedUnit, ...] = Field(min_length=1)


class PendingPublication(EvidenceModel):
    operation: RolloutIdentity
    predecessor: UUID | None
    candidate_digest: Digest
    challenge: UUID
    units: tuple[PublishedUnit, ...] = Field(min_length=1)


class WriterPublication(EvidenceModel):
    version: Literal[2] = 2
    current: CommittedPublication | None = None
    pending: PendingPublication | None = None

    @model_validator(mode="after")
    def unique_units(self) -> Self:
        for record in (self.current, self.pending):
            if record is None:
                continue
            keys = [(unit.machine, unit.home) for unit in record.units]
            if keys != sorted(keys) or len(keys) != len(set(keys)):
                raise ValueError("published units must be unique and sorted")
        if self.pending is not None:
            predecessor = self.current.publication_id if self.current is not None else None
            if self.pending.predecessor != predecessor:
                raise ValueError("pending publication has a different predecessor")
        return self


def _locked_publication(conn: psycopg.Connection) -> WriterPublication:
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise ManagedWriterBarrierError("publication requires a caller-owned transaction")
    row = conn.execute(
        "SELECT managed_writer_evidence FROM deployment_state WHERE id=1 FOR UPDATE"
    ).fetchone()
    if row is None:
        raise ManagedWriterBarrierError("deployment state is missing")
    if row[0] is None:
        return WriterPublication()
    # Strict JSON parsing preserves UUID/datetime wire types while rejecting
    # unknown versions. A historical v1 collection is NEVER a current permit.
    return WriterPublication.model_validate_json(json.dumps(row[0]))


def _store(conn: psycopg.Connection, publication: WriterPublication) -> None:
    conn.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s WHERE id=1",
        (Jsonb(publication.model_dump(mode="json")),),
    )


def begin_pending_publication(
    conn: psycopg.Connection,
    pending: PendingPublication,
) -> None:
    """Preserve current but freeze births before the existing updater mutates.

    This accepts expected inventory only, not permission. No filesystem/network
    calls may occur while the caller retains deployment -> registry row locks.
    A crash leaves pending in place even after lease expiry; only explicit
    verified completion/recovery may clear it. A new holder cannot overwrite it.
    """
    lock_rollout(conn, pending.operation)
    state = _locked_publication(conn)
    registered = lock_registered_units(conn)
    lock_rollout(conn, pending.operation)
    proposed = WriterPublication(current=state.current, pending=pending)
    if {(unit.machine, unit.home) for unit in pending.units} != registered:
        raise ManagedWriterBarrierError("prepared publication omits registered units")
    if state.pending is not None and state.pending != pending:
        raise ManagedWriterBarrierError("another pending publication requires explicit recovery")
    _store(conn, proposed)


def require_current_publication(
    conn: psycopg.Connection,
    actual: PublishedUnit,
    *,
    selector_artifact_digest: str,
    selector_manifest_digest: str,
) -> UUID:
    """Check actual loaded/selector facts BEFORE acquiring any agent row lock.

    Caller reads immutable loaded-image and canonical selector facts through the
    verified runtime consumer, never a remote label or installed SHA. The DB lock
    remains held through actual admission so begin_pending cannot race a birth.
    A committed publication outlives its originating lease but is not liveness.
    """
    state = _locked_publication(conn)
    registered = lock_registered_units(conn)
    if state.pending is not None or state.current is None:
        raise ManagedWriterBarrierError("runtime publication is unknown or transitioning")
    if {(unit.machine, unit.home) for unit in state.current.units} != registered:
        raise ManagedWriterBarrierError("registered units changed after publication")
    if actual not in state.current.units:
        raise ManagedWriterBarrierError("loaded unit does not match current publication")
    if (selector_artifact_digest, selector_manifest_digest) != (
        actual.artifact_digest,
        actual.manifest_digest,
    ):
        raise ManagedWriterBarrierError("loaded image and canonical selector differ")
    # Ordinary startup must also refuse an operation which has acquired its old
    # schema lease but has not yet persisted versioned pending inventory.
    row = conn.execute("SELECT phase FROM deployment_state WHERE id=1").fetchone()
    if row is None or row[0] != "stable":
        raise ManagedWriterBarrierError("deployment is not settled for ordinary admission")
    return state.current.publication_id
