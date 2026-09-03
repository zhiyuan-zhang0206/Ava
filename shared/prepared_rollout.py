"""Exact dispatch binding for the existing detached per-unit updater.

The native coordinator is the sole caller of create_prepared_operation. Every
participant retains its first returned operation identity in the local handoff;
subsequent checks never discover or attach to a replacement operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from pydantic import AwareDatetime

from shared.managed_writer_barrier import (
    ManagedWriterBarrierError,
    ManagedWriterCollection,
    RolloutIdentity,
    lock_registered_units,
    lock_rollout,
)
from shared.managed_writer_publication import (
    NormalStartPlan,
    PendingPublication,
    PreparedDispatch,
    PreparedUnitPreflight,
    PublishedUnit,
    WriterPublication,
    _locked_publication,
    _store,
    recover_pending_publication,
)


@dataclass(frozen=True)
class PreparedBlockage:
    """The locked pending operation and deployment row a coordinator may recover."""

    operation: RolloutIdentity | None
    predecessor: UUID | None
    phase: str
    holder: str | None
    acquired_at: AwareDatetime | None
    expires_at: AwareDatetime | None
    target_sha: str | None


def create_prepared_operation(
    conn: psycopg.Connection,
    *,
    dispatch: PreparedDispatch,
    plan: NormalStartPlan,
    target_sha: str,
    holder: str,
) -> RolloutIdentity:
    """Acquire once from stable state, never reclaim an expired old operation.

    Caller is the locally verified native coordinator child, not a role in an
    RPC body. The database independently verifies its exact registered gateway
    home and the complete roster. No filesystem or native calls occur locked.
    """
    state = _locked_publication(conn)
    registered = lock_registered_units(conn)
    units = tuple(entry.unit for entry in plan.units)
    if (
        dispatch.preflights
        or dispatch.coordinator not in units
        or {(unit.machine, unit.home) for unit in units} != registered
    ):
        raise ManagedWriterBarrierError("prepared dispatch must cover every registered unit")
    gateways = conn.execute(
        "SELECT machine_name, home FROM machine_units WHERE serve_gateway "
        "ORDER BY machine_name, home"
    ).fetchall()
    if gateways != [(dispatch.coordinator.machine, dispatch.coordinator.home)]:
        raise ManagedWriterBarrierError("prepared coordinator is not the unique gateway unit")
    row = conn.execute(
        "SELECT phase, holder, acquired_at, expires_at, clock_timestamp() "
        "FROM deployment_state WHERE id=1"
    ).fetchone()
    if row is None:
        raise ManagedWriterBarrierError("deployment state is absent")
    phase, old_holder, acquired, expires, now = row
    if (
        state.pending is not None
        or phase != "stable"
        or old_holder is not None
        or acquired is not None
        or expires is not None
        or dispatch.valid_until <= now
    ):
        raise ManagedWriterBarrierError("old operation requires explicit recovery before dispatch")
    operation = RolloutIdentity(holder=holder, acquired_at=now, target_sha=target_sha)
    pending = PendingPublication(
        operation=operation,
        predecessor=None if state.current is None else state.current.publication_id,
        candidate_digest=dispatch.request_digest,
        challenge=dispatch.request_id,
        units=units,
        normal_start_plan=plan,
        dispatch=dispatch,
    )
    conn.execute(
        "UPDATE deployment_state SET phase='updating',kind='rollout',holder=%s,"
        "acquired_at=%s,expires_at=%s,target_sha=%s,note=NULL,outcome=NULL,"
        "started_at=%s,ended_at=NULL WHERE id=1",
        (holder, now, dispatch.valid_until, target_sha, now),
    )
    _store(conn, WriterPublication(current=state.current, pending=pending))
    return operation


def read_prepared_blockage(conn: psycopg.Connection) -> PreparedBlockage:
    """Read the coordinator's exact blocked operation in its owned transaction.

    This authorizes no transition and refuses a missing deployment row. Its
    returned identity is only a predecessor candidate for explicit recovery.
    """
    state = _locked_publication(conn)
    row = conn.execute(
        "SELECT phase, holder, acquired_at, expires_at, target_sha FROM deployment_state WHERE id=1"
    ).fetchone()
    if row is None:
        raise ManagedWriterBarrierError("deployment state is missing")
    pending = state.pending
    return PreparedBlockage(
        operation=None if pending is None else pending.operation,
        predecessor=None if state.current is None else state.current.publication_id,
        phase=row[0],
        holder=row[1],
        acquired_at=row[2],
        expires_at=row[3],
        target_sha=row[4],
    )


def recover_prepared_operation(
    conn: psycopg.Connection,
    *,
    abandoned: RolloutIdentity,
    dispatch: PreparedDispatch,
    plan: NormalStartPlan,
    target_sha: str,
    fresh_collection: ManagedWriterCollection,
) -> RolloutIdentity:
    """Replace one exact abandoned pending operation in a coordinator transaction.

    Only the verified coordinator may call this after staging a fresh all-unit
    writer closure. The closure supplies the immutable replacement identity;
    this function cross-checks its target against the validated plan, performs
    an exact predecessor CAS, and refuses any stale, partial, or mismatched
    recovery without publishing or starting a service.
    """
    units = tuple(entry.unit for entry in plan.units)
    if dispatch.preflights or dispatch.coordinator not in units:
        raise ManagedWriterBarrierError("prepared recovery must begin without unit preflights")
    state = _locked_publication(conn)
    if state.pending is None or state.pending.operation != abandoned:
        raise ManagedWriterBarrierError("abandoned pending operation no longer matches")
    registered = lock_registered_units(conn)
    if {(unit.machine, unit.home) for unit in units} != registered:
        raise ManagedWriterBarrierError("prepared recovery must cover every registered unit")
    gateways = conn.execute(
        "SELECT machine_name, home FROM machine_units WHERE serve_gateway "
        "ORDER BY machine_name, home"
    ).fetchall()
    if gateways != [(dispatch.coordinator.machine, dispatch.coordinator.home)]:
        raise ManagedWriterBarrierError("prepared coordinator is not the unique gateway unit")
    row = conn.execute("SELECT clock_timestamp() FROM deployment_state WHERE id=1").fetchone()
    if row is None:
        raise ManagedWriterBarrierError("deployment state is missing")
    now = row[0]
    if dispatch.valid_until <= now:
        raise ManagedWriterBarrierError("prepared recovery deadline expired")
    new_operation = fresh_collection.operation
    if (
        new_operation == abandoned
        or new_operation.holder == abandoned.holder
        or new_operation.target_sha != target_sha
        or fresh_collection.challenge != dispatch.request_id
        or fresh_collection.candidate_digest != dispatch.request_digest
    ):
        raise ManagedWriterBarrierError(
            "prepared recovery collection has a different operation or target"
        )
    cursor = conn.execute(
        "UPDATE deployment_state SET phase='updating',kind='rollout',holder=%s,"
        "acquired_at=%s,expires_at=%s,target_sha=%s,started_at=%s,ended_at=NULL,"
        "note=NULL,outcome=NULL WHERE id=1 AND phase='updating' AND holder=%s "
        "AND acquired_at=%s AND target_sha=%s",
        (
            new_operation.holder,
            new_operation.acquired_at,
            dispatch.valid_until,
            new_operation.target_sha,
            now,
            abandoned.holder,
            abandoned.acquired_at,
            abandoned.target_sha,
        ),
    )
    if cursor.rowcount != 1:
        raise ManagedWriterBarrierError("prepared predecessor CAS failed")
    replacement = PendingPublication(
        operation=new_operation,
        predecessor=None if state.current is None else state.current.publication_id,
        candidate_digest=dispatch.request_digest,
        challenge=dispatch.request_id,
        units=units,
        normal_start_plan=plan,
        dispatch=dispatch,
    )
    recover_pending_publication(conn, abandoned, replacement, fresh_collection)
    return new_operation


def bind_prepared_participant(
    conn: psycopg.Connection,
    *,
    request_id: UUID,
    request_digest: str,
    valid_until: datetime,
    unit: PublishedUnit,
) -> RolloutIdentity:
    """First binding only, to an exact immutable dispatch (never latest/SHA)."""
    pending = _locked_publication(conn).pending
    if pending is None:
        raise ManagedWriterBarrierError("the exact prepared dispatch has not been published")
    dispatch = pending.dispatch
    if (
        dispatch is None
        or dispatch.request_id != request_id
        or dispatch.request_digest != request_digest
        or dispatch.valid_until != valid_until
        or pending.candidate_digest != request_digest
        or pending.challenge != request_id
        or unit not in pending.units
    ):
        raise ManagedWriterBarrierError("participant belongs to a different prepared request")
    lock_registered_units(conn)
    now = lock_rollout(conn, pending.operation)
    if now >= valid_until:
        raise ManagedWriterBarrierError("prepared dispatch deadline expired")
    return pending.operation


def require_prepared_dispatch(
    conn: psycopg.Connection,
    operation: RolloutIdentity,
    request_id: UUID,
    request_digest: str,
    unit: PublishedUnit,
) -> PendingPublication:
    """Fresh authority before each effect, using the already-bound identity."""
    lock_rollout(conn, operation)
    pending = _locked_publication(conn).pending
    if pending is None or pending.operation != operation or pending.dispatch is None:
        raise ManagedWriterBarrierError("prepared operation changed; reattachment is forbidden")
    registered = lock_registered_units(conn)
    now = lock_rollout(conn, operation)
    dispatch = pending.dispatch
    if (
        pending.challenge != request_id
        or dispatch.request_id != request_id
        or pending.candidate_digest != request_digest
        or dispatch.request_digest != request_digest
        or now >= dispatch.valid_until
        or unit not in pending.units
        or {(item.machine, item.home) for item in pending.units} != registered
    ):
        raise ManagedWriterBarrierError("prepared request, unit inventory or deadline changed")
    return pending


def record_prepared_preflight(
    conn: psycopg.Connection,
    operation: RolloutIdentity,
    request_id: UUID,
    evidence: PreparedUnitPreflight,
) -> None:
    """Persist actual local producer results; not a callable remote ready flag."""
    pending = require_prepared_dispatch(
        conn, operation, request_id, evidence.request_digest, evidence.unit
    )
    dispatch = pending.dispatch
    if dispatch is None:
        raise ManagedWriterBarrierError("prepared dispatch disappeared")
    now = lock_rollout(conn, operation)
    if not operation.acquired_at <= evidence.observed_at <= now:
        raise ManagedWriterBarrierError("preflight observation is outside this operation")
    previous = next((item for item in dispatch.preflights if item.unit == evidence.unit), None)
    if previous is not None:
        if previous != evidence:
            raise ManagedWriterBarrierError("unit preflight cannot be replaced in one dispatch")
        return
    entries = tuple(
        sorted(
            (*dispatch.preflights, evidence), key=lambda item: (item.unit.machine, item.unit.home)
        )
    )
    state = _locked_publication(conn)
    _store(
        conn,
        WriterPublication(
            current=state.current,
            pending=pending.model_copy(
                update={"dispatch": dispatch.model_copy(update={"preflights": entries})}
            ),
        ),
    )


def require_all_prepared_preflights(
    conn: psycopg.Connection,
    operation: RolloutIdentity,
    request_id: UUID,
    request_digest: str,
    unit: PublishedUnit,
) -> None:
    pending = require_prepared_dispatch(conn, operation, request_id, request_digest, unit)
    if pending.dispatch is None:
        raise ManagedWriterBarrierError("prepared dispatch disappeared")
    if tuple(item.unit for item in pending.dispatch.preflights) != pending.units:
        raise ManagedWriterBarrierError("a registered unit has not completed pre-stop preparation")
