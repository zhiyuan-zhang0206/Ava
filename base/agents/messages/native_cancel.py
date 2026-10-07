"""Guarded native cancellation identity, never a generic inbound command."""

from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.history.checkpoint import latest_checkpoint_id_in_transaction
from base.agents.incarnation.native_work import (
    NativeWorkRecord,
    decode_work,
    load_work,
    managed_resources,
    receiver,
)
from base.agents.incarnation.native_work_models import (
    NativeCancelAcceptance,
    NativeCancelMarker,
    NativeCancelOutcome,
    NativeWorkTarget,
    NativeWorkUncertainError,
)
from base.agents.incarnation.resources import IncarnationResources, decode_resources
from base.db.transaction import write_transaction
from base.native_process.runtime_incarnation import RuntimeIncarnation


class NativeCancelConflictError(ValueError):
    """A fixed operation/work identity is missing, unsupported or conflicts."""


def observe_native_work(pool: ConnectionPool, agent_id: int) -> NativeWorkTarget | None:
    """Advertise only actual ACTIVE work with the same admitted managed owner."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT w.id,w.agent_id,w.machine,w.generation,w.owner,w.protocol,w.phase,w.transfer_chain,"
            "m.incarnation_resources FROM agents_meta m "
            "JOIN native_graph_work w ON w.id=m.native_work_id WHERE m.id=%s "
            "AND w.phase='active' AND w.machine=m.machine AND w.generation=m.runtime_generation "
            "AND w.owner=m.runtime_owner AND m.runtime_kind='hosted' AND m.status='running' "
            "AND m.lease_expires_at>clock_timestamp() "
            "AND NOT EXISTS(SELECT 1 FROM agent_impersonations p WHERE p.agent_id=m.id AND p.status='active')",
            (agent_id,),
        ).fetchone()
    if row is None:
        return None
    work = decode_work(row[:8])
    return work.target if managed_resources(row[8], work.target) else None


def accept_native_cancel(
    pool: ConnectionPool, key: str, agent_id: int, target: NativeWorkTarget
) -> NativeCancelAcceptance:
    """Lookup immutable acceptance before mutable eligibility in one transaction."""
    if agent_id != target.agent_id:
        raise NativeCancelConflictError("native cancel target belongs to another agent")
    payload = target.model_dump(mode="json")
    with write_transaction(pool) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
        previous = conn.execute(
            "SELECT request,acceptance FROM native_cancel_commands WHERE operation_key=%s",
            (key,),
        ).fetchone()
        if previous is not None:
            if previous[0] != payload:
                raise NativeCancelConflictError("native cancel key was used for another target")
            return NativeCancelAcceptance.model_validate(previous[1])
        row = conn.execute(
            "SELECT w.id,w.agent_id,w.machine,w.generation,w.owner,w.protocol,w.phase,w.transfer_chain,"
            "m.incarnation_resources FROM agents_meta m "
            "JOIN native_graph_work w ON w.id=m.native_work_id WHERE m.id=%s "
            "AND w.id=%s AND w.phase='active' AND w.machine=m.machine "
            "AND w.generation=m.runtime_generation AND w.owner=m.runtime_owner "
            "AND m.runtime_kind='hosted' AND m.status='running' AND m.lease_expires_at>clock_timestamp() "
            "FOR UPDATE OF m,w",
            (agent_id, target.work_id),
        ).fetchone()
        if (
            row is None
            or decode_work(row[:8]).target != target
            or not managed_resources(row[8], target)
        ):
            raise NativeCancelConflictError("observed native work is not eligible")
        lease = conn.execute(
            "SELECT status FROM agent_impersonations WHERE agent_id=%s "
            "AND status IN ('requested','accepted','active') FOR UPDATE",
            (agent_id,),
        ).fetchall()
        if any(item[0] == "active" for item in lease):
            raise NativeCancelConflictError("external impersonation owns native control")
        if conn.execute(
            "SELECT 1 FROM native_cancel_commands WHERE work_id=%s", (target.work_id,)
        ).fetchone():
            raise NativeCancelConflictError("native work already has a cancel intent")
        accepted = NativeCancelAcceptance(command_id=uuid4(), target=target)
        conn.execute(
            "INSERT INTO native_cancel_commands(id,work_id,agent_id,operation_key,request,acceptance) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                accepted.command_id,
                target.work_id,
                agent_id,
                key,
                Jsonb(payload),
                Jsonb(accepted.model_dump(mode="json")),
            ),
        )
        return accepted


async def pending_native_cancel(
    conn: psycopg.AsyncConnection, target: NativeWorkTarget
) -> NativeCancelMarker | None:
    row = await (
        await conn.execute(
            "SELECT c.id,c.acceptance FROM native_cancel_commands c JOIN native_graph_work w ON w.id=c.work_id "
            "JOIN agents_meta m ON m.native_work_id=w.id WHERE c.work_id=%s "
            "AND c.outcome IN ('accepted','uncertain') AND w.phase IN ('active','uncertain') "
            "AND m.id=%s AND m.machine=%s AND m.runtime_generation=%s AND m.runtime_owner=%s",
            (target.work_id, target.agent_id, target.machine, target.generation, target.owner),
        )
    ).fetchone()
    if row is None:
        return None
    acceptance = NativeCancelAcceptance.model_validate(row[1])
    if acceptance.target != target or acceptance.command_id != row[0]:
        raise NativeWorkUncertainError("native cancel stored target differs from original work")
    return NativeCancelMarker(command_id=row[0], target=target)


async def require_cancel_receiver(
    conn: psycopg.AsyncConnection, work: NativeWorkRecord, incarnation: RuntimeIncarnation
) -> None:
    """Fence ACK to the original owner or the complete certified transfer chain."""
    row = await (
        await conn.execute(
            "SELECT machine,runtime_generation,runtime_owner,incarnation_resources FROM agents_meta "
            "WHERE id=%s AND native_work_id=%s AND runtime_kind='hosted' "
            "AND lease_expires_at>clock_timestamp() FOR UPDATE",
            (incarnation.agent_id, work.target.work_id),
        )
    ).fetchone()
    if row is None or incarnation.agent_id != work.target.agent_id or row[:3] != receiver(work):
        raise NativeWorkUncertainError("native cancel receiver lacks certified original authority")
    if row[1:3] != (incarnation.generation, incarnation.owner) or row[3] is None:
        raise NativeWorkUncertainError("native cancel receiver was replaced")
    resources = decode_resources(row[3])
    if (
        not isinstance(resources, IncarnationResources)
        or (resources.generation, resources.owner) != (incarnation.generation, incarnation.owner)
        or resources.host_process is None
        or resources.requests
        or resources.frozen_by is not None
    ):
        raise NativeWorkUncertainError("native cancel continuation resources remain unresolved")


async def _require_completion_checkpoint(
    conn: psycopg.AsyncConnection,
    work: NativeWorkRecord,
    outcome: NativeCancelOutcome,
    checkpoint_id: str | None,
    recovery_checkpoint_id: str | None,
) -> None:
    """Match execution or recovery proof to the latest head under the owner lock."""
    if outcome is NativeCancelOutcome.APPLIED and not checkpoint_id:
        raise NativeWorkUncertainError("native cancel has no committed checkpoint identity")
    if (
        outcome is NativeCancelOutcome.APPLIED
        and await latest_checkpoint_id_in_transaction(conn, work.target.agent_id) != checkpoint_id
    ):
        raise NativeWorkUncertainError("native cancel checkpoint evidence was superseded")
    if outcome is NativeCancelOutcome.RECOVERED_STOPPED:
        if not work.transfers:
            raise NativeWorkUncertainError("native cancel has no certified predecessor stop")
        if checkpoint_id is not None or not recovery_checkpoint_id:
            raise NativeWorkUncertainError("native recovery has no committed consumer pause")
        if (
            await latest_checkpoint_id_in_transaction(conn, work.target.agent_id)
            != recovery_checkpoint_id
        ):
            raise NativeWorkUncertainError("native recovery pause evidence was superseded")


async def finish_native_cancel(
    conn: psycopg.AsyncConnection,
    incarnation: RuntimeIncarnation,
    marker: NativeCancelMarker,
    *,
    outcome: NativeCancelOutcome,
    checkpoint_id: str | None,
    recovery_checkpoint_id: str | None = None,
) -> None:
    """CAS execution evidence only after cold checkpoint/actual-resource proof."""
    if outcome not in (NativeCancelOutcome.APPLIED, NativeCancelOutcome.RECOVERED_STOPPED):
        raise ValueError("native cancel completion requires a terminal evidence outcome")
    original = await (
        await conn.execute(
            "SELECT acceptance,outcome FROM native_cancel_commands WHERE id=%s AND work_id=%s",
            (marker.command_id, marker.target.work_id),
        )
    ).fetchone()
    if original is None or NativeCancelAcceptance.model_validate(
        original[0]
    ) != NativeCancelAcceptance(command_id=marker.command_id, target=marker.target):
        raise NativeWorkUncertainError("native cancel original command evidence is missing")
    if NativeCancelOutcome(original[1]) in (
        NativeCancelOutcome.APPLIED,
        NativeCancelOutcome.RECOVERED_STOPPED,
    ):
        return
    await conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (incarnation.agent_id,))
    work = await load_work(conn, marker.target.work_id, lock=True)
    if work is None or work.target != marker.target:
        raise NativeWorkUncertainError("native cancel original work evidence is missing")
    await require_cancel_receiver(conn, work, incarnation)
    await _require_completion_checkpoint(conn, work, outcome, checkpoint_id, recovery_checkpoint_id)
    cursor = await conn.execute(
        "UPDATE native_cancel_commands SET outcome=%s,checkpoint_id=%s,recovery_checkpoint_id=%s,settled_at=now() "
        "WHERE id=%s AND work_id=%s AND outcome IN ('accepted','uncertain') RETURNING id",
        (
            outcome.value,
            checkpoint_id,
            recovery_checkpoint_id,
            marker.command_id,
            marker.target.work_id,
        ),
    )
    if await cursor.fetchone() is None:
        row = await (
            await conn.execute(
                "SELECT outcome FROM native_cancel_commands WHERE id=%s AND work_id=%s",
                (marker.command_id, marker.target.work_id),
            )
        ).fetchone()
        if row is None or NativeCancelOutcome(row[0]) not in (
            NativeCancelOutcome.APPLIED,
            NativeCancelOutcome.RECOVERED_STOPPED,
        ):
            raise NativeWorkUncertainError("native cancel original command evidence is missing")
    await conn.execute(
        "UPDATE native_graph_work SET phase='settled',settled_checkpoint_id=%s,ended_at=now() WHERE id=%s",
        (checkpoint_id or recovery_checkpoint_id, marker.target.work_id),
    )
