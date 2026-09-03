"""Current release publication and pending rollout share one evidence field.

Only the existing rollout's verified producers may supply closure/readback facts.
These storage contracts do not authenticate caller-created Python values, attest
unregistered credential holders, or activate caller protocol support by themselves.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
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
    ManagedWriterCollection,
    RolloutIdentity,
    lock_registered_units,
    lock_rollout,
    validate_collection_for_write,
)
from shared.managed_writer_observation import ExpectedProcess


@dataclass(frozen=True)
class LegacyProtocolZero:
    """Known never-enabled publication; preserve existing protocol-zero behavior."""


@dataclass(frozen=True)
class DeferredAdmission:
    """Do not admit a new runtime; retain agent state and queued inbound work."""


@dataclass(frozen=True)
class CurrentAdmission:
    publication_id: UUID


AdmissionDecision = LegacyProtocolZero | DeferredAdmission | CurrentAdmission
_ADMISSION_ROW = "SELECT managed_writer_evidence, phase FROM deployment_state WHERE id=1 FOR UPDATE"
_UNIT_LOCK = "LOCK TABLE machine_units, machines IN SHARE MODE"
_UNITS = "SELECT machine_name, home FROM machine_units ORDER BY machine_name, home"
_MACHINES = "SELECT name FROM machines"


class PublishedUnit(ManagedUnit):
    """Bind observer expectations and the full prepared receipt separately.

    ``inventory_digest`` identifies the narrower ``ExpectedUnitWriters`` tuple
    returned by the unit observer. ``prepared_receipt_digest`` binds the sealed
    receipt that also retains service-only roster declarations.
    """

    prepared_receipt_digest: Digest
    artifact_digest: Digest
    manifest_digest: Digest


class NormalService(EvidenceModel):
    session: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    module: str | None = Field(default=None, min_length=1, max_length=512)
    executable: str = Field(min_length=1, max_length=4096)
    entrypoint: str = Field(min_length=1, max_length=4096)
    command_digest: Digest


class CandidateUnitPlan(EvidenceModel):
    unit: PublishedUnit
    services: tuple[NormalService, ...] = Field(min_length=1)
    previous_selector_digest: Digest | None
    selector_digest: Digest

    @model_validator(mode="after")
    def ordered_services(self) -> Self:
        names = [service.session for service in self.services]
        if names != sorted(set(names)):
            raise ValueError("normal services must be unique and sorted")
        root = self.unit.home + "/releases/" + self.unit.artifact_digest + "/"
        if not self.unit.home.startswith("/"):
            raise ValueError("normal candidate startup has no Windows platform adapter")
        for service in self.services:
            if service.session.startswith("ava-agent-") and service.session != "ava-agent-host":
                raise ValueError("normal service startup cannot authorize an agent session")
            for path in (service.executable, service.entrypoint):
                if not path.startswith(root) or ".." in path.split("/"):
                    raise ValueError("normal service command must belong to its retained image")
        selector = {
            "version": 2,
            "artifact_digest": self.unit.artifact_digest,
            "manifest_digest": self.unit.manifest_digest,
            "prepared_receipt_digest": self.unit.prepared_receipt_digest,
        }
        encoded = (json.dumps(selector, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if hashlib.sha256(encoded).hexdigest() != self.selector_digest:
            raise ValueError("selector digest does not encode the exact v2 unit tuple")
        return self


class NormalStartPlan(EvidenceModel):
    schema_digest: Digest
    applied_names: tuple[str, ...]
    units: tuple[CandidateUnitPlan, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_inventory(self) -> Self:
        keys = [(entry.unit.machine, entry.unit.home) for entry in self.units]
        if keys != sorted(set(keys)) or list(self.applied_names) != sorted(set(self.applied_names)):
            raise ValueError("normal startup inventory and migration names must be sorted sets")
        return self


class PendingMigrationReceipt(EvidenceModel):
    operation: RolloutIdentity
    challenge: UUID
    schema_digest: Digest
    applied_names: tuple[str, ...]
    verified_at: AwareDatetime


class SelectorReadback(EvidenceModel):
    unit: PublishedUnit
    challenge: UUID
    previous_digest: Digest | None
    current_digest: Digest
    observed_at: AwareDatetime
    valid_until: AwareDatetime


class NormalServiceReadback(EvidenceModel):
    service: NormalService
    supervisor: ExpectedProcess
    child: ExpectedProcess
    loaded_module: str | None = Field(default=None, min_length=1, max_length=4096)
    executable: str = Field(min_length=1, max_length=4096)
    entrypoint: str = Field(min_length=1, max_length=4096)
    artifact_digest: Digest
    manifest_digest: Digest
    readiness: Literal["normal"]
    challenge: UUID
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    observation_digest: Digest


class UnitActivationReadback(EvidenceModel):
    selector: SelectorReadback
    services: tuple[NormalServiceReadback, ...] = Field(min_length=1)


class CommittedPublication(EvidenceModel):
    publication_id: UUID
    operation: RolloutIdentity
    committed_at: AwareDatetime
    units: tuple[PublishedUnit, ...] = Field(min_length=1)
    activation_digest: Digest | None = None
    activation_challenge: UUID | None = None


class PendingPublication(EvidenceModel):
    operation: RolloutIdentity
    predecessor: UUID | None
    candidate_digest: Digest
    challenge: UUID
    units: tuple[PublishedUnit, ...] = Field(min_length=1)
    collection: ManagedWriterCollection | None = None
    normal_start_plan: NormalStartPlan | None = None
    migration: PendingMigrationReceipt | None = None
    unit_readbacks: tuple[UnitActivationReadback, ...] = ()


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
            plan = self.pending.normal_start_plan
            if plan is not None and tuple(entry.unit for entry in plan.units) != self.pending.units:
                raise ValueError("normal startup plan must cover the exact prepared units")
            readback_units = [item.selector.unit for item in self.pending.unit_readbacks]
            keys = [(unit.machine, unit.home) for unit in readback_units]
            if keys != sorted(set(keys)) or any(
                unit not in self.pending.units for unit in readback_units
            ):
                raise ValueError("pending readbacks must be an ordered unique prepared-unit subset")
            collection = self.pending.collection
            if collection is not None and (
                collection.operation != self.pending.operation
                or collection.challenge != self.pending.challenge
                or collection.candidate_digest != self.pending.candidate_digest
            ):
                raise ValueError("pending collection belongs to a different operation")
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
    if pending.collection is not None or pending.migration is not None or pending.unit_readbacks:
        raise ManagedWriterBarrierError("prepare cannot import a cached collection")
    lock_rollout(conn, pending.operation)
    state = _locked_publication(conn)
    registered = lock_registered_units(conn)
    lock_rollout(conn, pending.operation)
    proposed = WriterPublication(current=state.current, pending=pending)
    if {(unit.machine, unit.home) for unit in pending.units} != registered:
        raise ManagedWriterBarrierError("prepared publication omits registered units")
    if state.pending is not None:
        if (
            state.pending.model_copy(
                update={"collection": None, "migration": None, "unit_readbacks": ()}
            )
            != pending
        ):
            raise ManagedWriterBarrierError(
                "another pending publication requires explicit recovery"
            )
        # A same-operation prepare retry must not erase evidence already adopted
        # after stop. Continue from durable state, not from the caller's old copy.
        return
    _store(conn, proposed)


def adopt_pending_collection(conn: psycopg.Connection, collection: ManagedWriterCollection) -> None:
    """Store fresh post-stop facts without promoting them to a current release.

    The expected challenge and full receipt digests come from locked pending
    state, not from a replayed collection's own assertions. Actual collector
    provenance is the existing updater's responsibility; this is not an API.
    """
    lock_rollout(conn, collection.operation)
    state = _locked_publication(conn)
    pending = state.pending
    if pending is None:
        raise ManagedWriterBarrierError("no prepared pending operation exists")
    prepared = tuple(
        ManagedUnit(
            machine=unit.machine,
            home=unit.home,
            inventory_digest=unit.prepared_receipt_digest,
        )
        for unit in pending.units
    )
    validate_collection_for_write(
        conn,
        collection,
        operation=pending.operation,
        candidate_digest=pending.candidate_digest,
        expected_challenge=pending.challenge,
        prepared_units=prepared,
    )
    if pending.collection is not None and pending.collection != collection:
        raise ManagedWriterBarrierError("pending collection cannot be silently replaced")
    _store(
        conn,
        WriterPublication(
            current=state.current, pending=pending.model_copy(update={"collection": collection})
        ),
    )


def recover_pending_publication(
    conn: psycopg.Connection,
    abandoned: RolloutIdentity,
    replacement: PendingPublication,
    fresh_collection: ManagedWriterCollection,
) -> None:
    """Explicit recovery CAS requires fresh complete closure under the NEW lease.

    TTL expiry alone is not old-holder exit evidence and cannot clear pending.
    The existing takeover producer must include that holder among managed writers
    and positively establish its exit before acquiring its replacement lease.
    Recovery preserves current and keeps births frozen; it does not publish.
    """
    if (
        replacement.collection is not None
        or replacement.migration is not None
        or replacement.unit_readbacks
        or replacement.operation == abandoned
    ):
        raise ManagedWriterBarrierError("recovery requires a new operation and fresh collection")
    lock_rollout(conn, replacement.operation)
    state = _locked_publication(conn)
    if state.pending is None or state.pending.operation != abandoned:
        raise ManagedWriterBarrierError("abandoned pending operation no longer matches")
    if replacement.challenge == state.pending.challenge:
        raise ManagedWriterBarrierError("recovery must issue a new observation challenge")
    prepared = tuple(
        ManagedUnit(
            machine=unit.machine,
            home=unit.home,
            inventory_digest=unit.prepared_receipt_digest,
        )
        for unit in replacement.units
    )
    validate_collection_for_write(
        conn,
        fresh_collection,
        operation=replacement.operation,
        candidate_digest=replacement.candidate_digest,
        expected_challenge=replacement.challenge,
        prepared_units=prepared,
    )
    _store(
        conn,
        WriterPublication(
            current=state.current,
            pending=replacement.model_copy(update={"collection": fresh_collection}),
        ),
    )


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


def _admission_state(row: tuple[object, ...] | None) -> WriterPublication | AdmissionDecision:
    if row is None:
        raise ManagedWriterBarrierError("deployment state is missing")
    evidence, phase = row
    if evidence is None:
        # SQL NULL is the migration's explicit never-enabled value, not a parse fallback.
        return LegacyProtocolZero() if phase == "stable" else DeferredAdmission()
    state = WriterPublication.model_validate_json(json.dumps(evidence))
    if state.pending is not None:
        return DeferredAdmission()
    if state.current is None:
        raise ManagedWriterBarrierError("publication evidence has no current or pending record")
    if phase != "stable":
        return DeferredAdmission()
    return state


def _current_admission(
    state: WriterPublication,
    units: set[tuple[str, str]],
    machines: set[str],
    actual: PublishedUnit | None,
    selector_artifact_digest: str | None,
    selector_manifest_digest: str | None,
) -> CurrentAdmission:
    current = state.current
    if current is None:
        raise ManagedWriterBarrierError("current publication is absent")
    if not units or machines != {machine for machine, _home in units}:
        raise ManagedWriterBarrierError("registered unit inventory is incomplete")
    if {(unit.machine, unit.home) for unit in current.units} != units:
        raise ManagedWriterBarrierError("registered units changed after publication")
    if actual is None or actual not in current.units:
        raise ManagedWriterBarrierError("trusted loaded unit facts are missing or mismatched")
    if (selector_artifact_digest, selector_manifest_digest) != (
        actual.artifact_digest,
        actual.manifest_digest,
    ):
        raise ManagedWriterBarrierError("loaded image and canonical selector differ")
    return CurrentAdmission(current.publication_id)


def publication_admission(
    conn: psycopg.Connection,
    actual: PublishedUnit | None = None,
    *,
    selector_artifact_digest: str | None = None,
    selector_manifest_digest: str | None = None,
) -> AdmissionDecision:
    """Classify under the caller's transaction before locking any agent row.

    Deferred and invalid evidence must never terminate/deadletter an agent.
    Runtime facts come from the verified consumer, not user input. This helper
    neither enables the new mode nor authenticates caller-created DTOs.
    """
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise ManagedWriterBarrierError("admission requires a caller-owned transaction")
    state = _admission_state(conn.execute(_ADMISSION_ROW).fetchone())
    if not isinstance(state, WriterPublication):
        return state
    conn.execute(_UNIT_LOCK)
    units = set(conn.execute(_UNITS).fetchall())
    machines = {row[0] for row in conn.execute(_MACHINES).fetchall()}
    return _current_admission(
        state, units, machines, actual, selector_artifact_digest, selector_manifest_digest
    )


async def publication_admission_async(
    conn: psycopg.AsyncConnection,
    actual: PublishedUnit | None = None,
    *,
    selector_artifact_digest: str | None = None,
    selector_manifest_digest: str | None = None,
) -> AdmissionDecision:
    """Async transport for the identical SQL, lock order and decision function."""
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise ManagedWriterBarrierError("admission requires a caller-owned transaction")
    state = _admission_state(await (await conn.execute(_ADMISSION_ROW)).fetchone())
    if not isinstance(state, WriterPublication):
        return state
    await conn.execute(_UNIT_LOCK)
    units = set(await (await conn.execute(_UNITS)).fetchall())
    machines = {row[0] for row in await (await conn.execute(_MACHINES)).fetchall()}
    return _current_admission(
        state, units, machines, actual, selector_artifact_digest, selector_manifest_digest
    )
