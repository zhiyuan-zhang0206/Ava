"""Typed managed-writer evidence and the existing rollout's transaction fence.

These checks do not attest arbitrary external holders of cluster credentials.
Collection must come from the rollout's actual process/job observer, not a
successful pause RPC. A local candidate receipt is evidence, never permission.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal, Self
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ManagedWriterBarrierError(RuntimeError):
    """The managed-writer closure or operation authority is unknown/stale."""


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RolloutIdentity(EvidenceModel):
    holder: str = Field(min_length=1, max_length=256)
    acquired_at: AwareDatetime
    target_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class ManagedUnit(EvidenceModel):
    machine: str = Field(min_length=1, max_length=128)
    home: str = Field(min_length=1, max_length=4096)
    inventory_digest: Digest

    @field_validator("home")
    @classmethod
    def normalized_absolute_home(cls, value: str) -> str:
        # Remote Windows paths must not be interpreted using the gateway's OS.
        path = PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or str(path) != value:
            raise ValueError("managed unit home must be normalized and absolute")
        if any(ord(char) < 32 for char in value):
            raise ValueError("managed unit home contains control characters")
        return value


class ManagedUnitClosure(EvidenceModel):
    unit: ManagedUnit
    boot_id: UUID
    observer_instance: UUID
    # Digest of exact PID/birth/session and OS-job observations, not argv/secrets.
    observation_digest: Digest
    outcome: Literal["old_writers_absent_relaunchers_fenced"]


class ManagedWriterCollection(EvidenceModel):
    """Non-authoritative journal input until locked, freshly revalidated adoption."""

    version: Literal[1] = 1
    operation: RolloutIdentity
    candidate_digest: Digest
    challenge: UUID
    collected_at: AwareDatetime
    valid_until: AwareDatetime
    units: tuple[ManagedUnitClosure, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_collection(self) -> Self:
        keys = [(entry.unit.machine, entry.unit.home) for entry in self.units]
        if len(keys) != len(set(keys)) or keys != sorted(keys):
            raise ValueError("managed unit closure must be unique and sorted")
        if self.collected_at < self.operation.acquired_at or self.valid_until <= self.collected_at:
            raise ValueError("managed writer collection has an invalid observation window")
        return self


def lock_rollout(conn: psycopg.Connection, operation: RolloutIdentity) -> datetime:
    """Fence a write using pre-existing columns, including before this migration.

    Caller owns a short transaction: no network or filesystem operations while
    locked. Lock order is deployment_state, unit inventory, then agent rows.
    The clock comparison occurs AFTER acquisition, not in the locking predicate.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise ManagedWriterBarrierError("managed writer checks require a caller-owned transaction")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM deployment_state WHERE id=1 FOR UPDATE")
        if cur.fetchone() is None:
            raise ManagedWriterBarrierError("deployment operation is missing")
        cur.execute(
            "SELECT clock_timestamp() FROM deployment_state WHERE id=1 "
            "AND phase='updating' AND kind='rollout' AND note IS NULL "
            "AND holder=%s AND acquired_at=%s AND target_sha=%s "
            "AND expires_at > clock_timestamp()",
            (operation.holder, operation.acquired_at, operation.target_sha),
        )
        row = cur.fetchone()
        if row is None:
            raise ManagedWriterBarrierError(
                "managed writer evidence does not own the current live rollout"
            )
        return row[0]


def lock_registered_units(conn: psycopg.Connection) -> set[tuple[str, str]]:
    """Read every registered unit, including stopped/paused/staging machines.

    Table SHARE locks also fence concurrent INSERT/DELETE (row locks cannot
    prevent a newly enrolled unit appearing between enumeration and adoption).
    No endpoint/URL is persisted in evidence. Orphan machine rows fail closed.
    """
    with conn.cursor() as cur:
        # register_self/mark_unit_stopped write units before composed machines.
        # Match their order rather than deadlocking an in-flight registration.
        cur.execute("LOCK TABLE machine_units, machines IN SHARE MODE")
        cur.execute("SELECT machine_name, home FROM machine_units ORDER BY machine_name, home")
        units = set(cur.fetchall())
        cur.execute("SELECT name FROM machines")
        machines = {row[0] for row in cur.fetchall()}
    if not units or machines != {machine for machine, _home in units}:
        raise ManagedWriterBarrierError(
            "registered machines and managed unit inventory are incomplete"
        )
    return units


def validate_collection_for_write(
    conn: psycopg.Connection,
    collection: ManagedWriterCollection,
    *,
    operation: RolloutIdentity,
    candidate_digest: str,
    expected_challenge: UUID,
    prepared_units: tuple[ManagedUnit, ...],
) -> None:
    """Revalidate fresh observation input before adopting it into the operation.

    This does not advertise a protocol or manufacture a positive observation.
    The collector must supply the outstanding challenge and actual prepared
    inventories; a cached local receipt alone is insufficient.
    """
    lock_rollout(conn, operation)
    units = lock_registered_units(conn)
    # Waiting for an inventory writer may outlive the deployment lease too.
    now = lock_rollout(conn, operation)
    if collection.operation != operation or collection.candidate_digest != candidate_digest:
        raise ManagedWriterBarrierError(
            "managed writer evidence belongs to another operation/candidate"
        )
    if collection.challenge != expected_challenge:
        raise ManagedWriterBarrierError("managed writer acknowledgement challenge was replayed")
    if not collection.collected_at <= now < collection.valid_until:
        raise ManagedWriterBarrierError("managed writer observation is expired or from the future")
    expected = {(unit.machine, unit.home): unit for unit in prepared_units}
    observed = {(entry.unit.machine, entry.unit.home): entry.unit for entry in collection.units}
    if len(expected) != len(prepared_units) or set(expected) != units or observed != expected:
        raise ManagedWriterBarrierError(
            "registered/prepared/observed managed writer inventory differs"
        )


def record_collection(
    conn: psycopg.Connection,
    collection: ManagedWriterCollection,
    *,
    operation: RolloutIdentity,
    candidate_digest: str,
    expected_challenge: UUID,
    prepared_units: tuple[ManagedUnit, ...],
) -> None:
    """Adopt freshly revalidated collection; never replace prior permission silently.

    The caller's transaction commits the observation. Admission still requires
    the live operation and actual runtime-image/owner validation; this record
    alone is not a reusable startup permit.
    """
    validate_collection_for_write(
        conn,
        collection,
        operation=operation,
        candidate_digest=candidate_digest,
        expected_challenge=expected_challenge,
        prepared_units=prepared_units,
    )
    payload = collection.model_dump(mode="json")
    with conn.cursor() as cur:
        cur.execute("SELECT managed_writer_evidence FROM deployment_state WHERE id=1")
        row = cur.fetchone()
        if row is None:
            raise ManagedWriterBarrierError("deployment operation is missing")
        if row[0] is not None and row[0] != payload:
            raise ManagedWriterBarrierError(
                "existing managed writer evidence requires explicit retirement"
            )
        cur.execute(
            "UPDATE deployment_state SET managed_writer_evidence=%s WHERE id=1",
            (Jsonb(payload),),
        )
