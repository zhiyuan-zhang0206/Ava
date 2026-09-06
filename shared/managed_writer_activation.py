"""Existing updater's pending service authorization and final publication.

No RPC accepts these values. The updater must obtain native/HTTP/selector facts
from its authenticated, exact-image observers before entering a short transaction.
Python DTO construction is not authentication. Agent births use the separate
publication admission gate and are never permitted by this service-only API.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

import psycopg

from shared.managed_writer_barrier import (
    ManagedUnit,
    ManagedWriterBarrierError,
    RolloutIdentity,
    lock_rollout,
    validate_collection_for_write,
)
from shared.managed_writer_publication import (
    CandidateUnitPlan,
    CommittedPublication,
    NormalService,
    NormalStartPlan,
    PendingMigrationReceipt,
    PendingPublication,
    PublishedUnit,
    WriterPublication,
    _locked_publication,
    _store,
)
from shared.managed_writer_publication import (
    NormalServiceReadback as NormalServiceReadback,
)
from shared.managed_writer_publication import (
    SelectorReadback as SelectorReadback,
)
from shared.managed_writer_publication import (
    UnitActivationReadback as UnitActivationReadback,
)


def pending_stage(
    conn: psycopg.Connection, operation: RolloutIdentity, challenge: UUID
) -> Literal["waiting_collection", "waiting_migration", "selector_allowed", "committed"]:
    """Bounded updater wait hint, not permission to perform an OS effect."""
    state = _locked_publication(conn)
    if state.pending is None:
        if (
            state.current is not None
            and state.current.operation == operation
            and state.current.activation_challenge == challenge
        ):
            return "committed"
        raise ManagedWriterBarrierError("operation has no matching publication")
    lock_rollout(conn, operation)
    pending = state.pending
    if pending.operation != operation or pending.challenge != challenge:
        raise ManagedWriterBarrierError("pending stage belongs to another operation")
    if pending.collection is None:
        return "waiting_collection"
    if pending.migration is None:
        return "waiting_migration"
    return "selector_allowed"


def require_pending_selector_change(
    conn: psycopg.Connection, operation: RolloutIdentity, challenge: UUID, unit: PublishedUnit
) -> CandidateUnitPlan:
    """Fresh service-only selector authorization before the existing local CAS."""
    _, pending, _ = _pending(conn, operation, challenge)
    _require_migration(conn, pending)
    _pending(conn, operation, challenge)
    plan = pending.normal_start_plan
    if plan is None:
        raise ManagedWriterBarrierError("normal startup plan is absent")
    for entry in plan.units:
        if entry.unit == unit:
            return entry
    raise ManagedWriterBarrierError("selector unit differs from full prepared inventory")


def _pending(
    conn: psycopg.Connection, operation: RolloutIdentity, challenge: UUID
) -> tuple[WriterPublication, PendingPublication, datetime]:
    lock_rollout(conn, operation)
    state = _locked_publication(conn)
    pending = state.pending
    if (
        pending is None
        or pending.operation != operation
        or pending.challenge != challenge
        or pending.normal_start_plan is None
        or pending.collection is None
    ):
        raise ManagedWriterBarrierError("pending normal startup authority is missing or mismatched")
    validate_collection_for_write(
        conn,
        pending.collection,
        operation=operation,
        candidate_digest=pending.candidate_digest,
        expected_challenge=challenge,
        prepared_units=tuple(
            ManagedUnit(
                machine=u.machine,
                home=u.home,
                inventory_digest=u.prepared_receipt_digest,
            )
            for u in pending.units
        ),
    )
    return state, pending, lock_rollout(conn, operation)


def _migration_names(conn: psycopg.Connection) -> tuple[str, ...]:
    # Keep the verified SET unchanged through the caller's commit. Updaters must
    # not run migrations while holding this startup/publication transaction.
    conn.execute("LOCK TABLE schema_migrations IN SHARE MODE")
    return tuple(row[0] for row in conn.execute("SELECT name FROM schema_migrations ORDER BY name"))


def require_pending_migration(
    conn: psycopg.Connection, operation: RolloutIdentity, challenge: UUID
) -> NormalStartPlan:
    """Authorize the existing migration runner after exact all-unit old-writer closure."""
    _, pending, _ = _pending(conn, operation, challenge)
    if pending.normal_start_plan is None:
        raise ManagedWriterBarrierError("normal startup plan is absent")
    return pending.normal_start_plan


def record_pending_migration(
    conn: psycopg.Connection, operation: RolloutIdentity, challenge: UUID
) -> PendingMigrationReceipt:
    """Read actual applied SQL names against the immutable verified candidate plan.

    The migration runner owns schema-content verification; this proves the tracked
    applied SET, not that arbitrary out-of-band DDL has never occurred.
    """
    state, pending, _ = _pending(conn, operation, challenge)
    plan = pending.normal_start_plan
    if plan is None:
        raise ManagedWriterBarrierError("normal startup plan is absent")
    if _migration_names(conn) != plan.applied_names:
        raise ManagedWriterBarrierError("actual migrations differ from the prepared schema SET")
    _, _, now = _pending(conn, operation, challenge)
    if pending.migration is not None:
        _require_migration(conn, pending)
        return pending.migration
    receipt = PendingMigrationReceipt(
        operation=operation,
        challenge=challenge,
        schema_digest=plan.schema_digest,
        applied_names=plan.applied_names,
        verified_at=now,
    )
    _store(
        conn,
        WriterPublication(
            current=state.current, pending=pending.model_copy(update={"migration": receipt})
        ),
    )
    return receipt


def _require_migration(conn: psycopg.Connection, pending: PendingPublication) -> None:
    plan, receipt = pending.normal_start_plan, pending.migration
    if (
        plan is None
        or receipt is None
        or receipt.operation != pending.operation
        or receipt.challenge != pending.challenge
        or receipt.schema_digest != plan.schema_digest
        or receipt.applied_names != plan.applied_names
        or receipt.verified_at < pending.operation.acquired_at
        or _migration_names(conn) != plan.applied_names
    ):
        raise ManagedWriterBarrierError("pending migration receipt is absent or stale")


def _selector(
    pending: PendingPublication, readback: SelectorReadback, now: datetime
) -> CandidateUnitPlan:
    plan = pending.normal_start_plan
    if plan is None:
        raise ManagedWriterBarrierError("normal startup plan is absent")
    matches = [entry for entry in plan.units if entry.unit == readback.unit]
    if len(matches) != 1:
        raise ManagedWriterBarrierError("selector unit differs from full prepared inventory")
    expected = matches[0]
    if (
        readback.challenge != pending.challenge
        or readback.previous_digest != expected.previous_selector_digest
        or readback.current_digest != expected.selector_digest
        or not pending.operation.acquired_at <= readback.observed_at <= now < readback.valid_until
    ):
        raise ManagedWriterBarrierError("selector CAS readback is mismatched, replayed or expired")
    return expected


def require_pending_candidate_start(
    conn: psycopg.Connection,
    operation: RolloutIdentity,
    challenge: UUID,
    selector: SelectorReadback,
    service: NormalService,
) -> NormalService:
    """Revalidate immediately before one normal-service effect, never agent work.

    Caller commits before OS work under its existing unit flock/deadline. This
    value is not a bearer token or a renewable lease; every subsequent effect
    must call again. Local updater serialization bridges DB to the OS effect.
    """
    _, pending, _ = _pending(conn, operation, challenge)
    _require_migration(conn, pending)
    _, _, now = _pending(conn, operation, challenge)
    expected = _selector(pending, selector, now)
    if service not in expected.services:
        raise ManagedWriterBarrierError("service is not in the prepared normal startup roster")
    return service


def _validate_unit_readback(
    pending: PendingPublication, item: UnitActivationReadback, now: datetime
) -> None:
    expected = _selector(pending, item.selector, now)
    if tuple(entry.service for entry in item.services) != expected.services:
        raise ManagedWriterBarrierError("normal service readbacks omit or duplicate a service")
    for entry in item.services:
        if (
            entry.readiness != "normal"
            or entry.challenge != pending.challenge
            or not pending.operation.acquired_at <= entry.observed_at <= now < entry.valid_until
        ):
            raise ManagedWriterBarrierError("normal service observation is stale or bootstrap-only")
        if (entry.artifact_digest, entry.manifest_digest) != (
            expected.unit.artifact_digest,
            expected.unit.manifest_digest,
        ):
            raise ManagedWriterBarrierError("running service belongs to another image")
        root = expected.unit.home + "/releases/" + expected.unit.artifact_digest + "/"
        paths = [entry.executable, entry.entrypoint]
        if entry.service.module is not None:
            if entry.loaded_module is None:
                raise ManagedWriterBarrierError("Python service has no loaded-module evidence")
            paths.append(entry.loaded_module)
        if (
            entry.executable != entry.service.executable
            or entry.entrypoint != entry.service.entrypoint
            or any(not path.startswith(root) or ".." in path.split("/") for path in paths)
        ):
            raise ManagedWriterBarrierError("normal service module is outside the loaded image")


def record_pending_unit_readback(
    conn: psycopg.Connection,
    operation: RolloutIdentity,
    challenge: UUID,
    readback: UnitActivationReadback,
) -> None:
    """Persist one trusted updater observation in the existing pending journal.

    Exact retries are idempotent; changed observations require explicit recovery,
    not overwriting an immutable result. Stored observations never gain freshness
    from being read back, and are revalidated before publication.
    """
    state, pending, _ = _pending(conn, operation, challenge)
    _require_migration(conn, pending)
    _, _, now = _pending(conn, operation, challenge)
    _validate_unit_readback(pending, readback, now)
    key = (readback.selector.unit.machine, readback.selector.unit.home)
    for previous in pending.unit_readbacks:
        if (previous.selector.unit.machine, previous.selector.unit.home) == key:
            if previous != readback:
                raise ManagedWriterBarrierError("pending unit readback cannot be silently replaced")
            return
    readbacks = tuple(
        sorted(
            (*pending.unit_readbacks, readback),
            key=lambda item: (item.selector.unit.machine, item.selector.unit.home),
        )
    )
    _store(
        conn,
        WriterPublication(
            current=state.current, pending=pending.model_copy(update={"unit_readbacks": readbacks})
        ),
    )


def read_pending_unit_readbacks(
    conn: psycopg.Connection,
    operation: RolloutIdentity,
    challenge: UUID,
) -> tuple[UnitActivationReadback, ...]:
    """Return freshly checked immutable progress; partial results are not ready."""
    _, pending, _ = _pending(conn, operation, challenge)
    _require_migration(conn, pending)
    _, _, now = _pending(conn, operation, challenge)
    for item in pending.unit_readbacks:
        _validate_unit_readback(pending, item, now)
    return pending.unit_readbacks


def commit_current(
    conn: psycopg.Connection,
    operation: RolloutIdentity,
    challenge: UUID,
    readbacks: tuple[UnitActivationReadback, ...],
) -> UUID:
    """Publish exact all-unit normal-service observations; preserve lease ownership.

    Rollout finalization remains the existing owner's responsibility. Until it
    marks phase stable, ordinary admission still refuses. Missing or unsupported
    evidence never falls back to legacy mode or to a hard-coded old release.
    """
    digest = hashlib.sha256(
        "".join(item.model_dump_json() for item in readbacks).encode()
    ).hexdigest()
    state = _locked_publication(conn)
    if state.pending is None and state.current is not None:
        current = state.current
        if (
            current.operation == operation
            and current.activation_challenge == challenge
            and current.activation_digest == digest
        ):
            return current.publication_id
        raise ManagedWriterBarrierError(
            "publication completion replay differs from committed evidence"
        )
    _, pending, _ = _pending(conn, operation, challenge)
    _require_migration(conn, pending)
    _, _, now = _pending(conn, operation, challenge)
    if tuple(item.selector.unit for item in readbacks) != pending.units:
        raise ManagedWriterBarrierError("publication needs exact ordered all-unit readbacks")
    if pending.unit_readbacks and pending.unit_readbacks != readbacks:
        raise ManagedWriterBarrierError("publication differs from the recorded unit readbacks")
    for item in readbacks:
        _validate_unit_readback(pending, item, now)
    publication_id = uuid4()
    current = CommittedPublication(
        publication_id=publication_id,
        operation=operation,
        committed_at=now,
        units=pending.units,
        activation_digest=digest,
        activation_challenge=challenge,
    )
    _store(conn, WriterPublication(current=current))
    return publication_id
