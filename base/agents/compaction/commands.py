"""HTTP domain acceptance for an actual host's retained compact observation."""

from uuid import uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.compaction.history import ADMISSION_SQL, PENDING_HISTORY_SQL
from base.agents.compaction.models import (
    CompactAcceptance,
    CompactConflictError,
    CompactOutcome,
    CompactStatus,
    CompactTarget,
)
from base.agents.incarnation.native_work import managed_resources
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.incarnation.resources import IncarnationResources, decode_resources
from base.db.transaction import write_transaction
from base.telemetry.audit_events import prepare_event_log, record_audit


def history_matches(conn: Connection, target: CompactTarget) -> bool:
    """Allow administrative projections, never changed message/compact channels."""
    row = conn.execute(
        "SELECT checkpoint->'channel_versions'->>'messages', "
        "checkpoint->'channel_versions'->>'compact' FROM checkpoints "
        "WHERE thread_id=%s AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1",
        (str(target.source.agent_id),),
    ).fetchone()
    anchor = conn.execute(
        "SELECT 1 FROM checkpoints WHERE thread_id=%s AND checkpoint_ns='' AND checkpoint_id=%s",
        (str(target.source.agent_id), target.checkpoint_id),
    ).fetchone()
    admission = conn.execute(ADMISSION_SQL, (target.source.agent_id,)).fetchone()
    pending = conn.execute(PENDING_HISTORY_SQL, (str(target.source.agent_id),) * 2).fetchone()
    return (
        pending is None
        and admission is None
        and anchor is not None
        and row
        == (
            target.messages_version,
            target.compact_channel_version,
        )
    )


def eligible(conn: Connection, target: CompactTarget, *, lock: bool) -> bool:
    source = target.source
    if lock:
        conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (source.agent_id,))
    row = conn.execute(
        "SELECT o.target,o.resources,m.incarnation_resources,w.phase,w.ended_at "  # noqa: S608 -- static lock suffix
        "FROM agents_meta m JOIN native_compact_observations o ON o.agent_id=m.id "
        "JOIN native_graph_work w ON w.id=o.work_id WHERE m.id=%s AND o.id=%s "
        "AND m.native_work_id=w.id AND w.id=%s AND m.machine=%s "
        "AND m.runtime_generation=%s AND m.runtime_owner=%s "
        "AND m.runtime_kind='hosted' AND m.status='idling' "
        "AND m.lease_expires_at>clock_timestamp() "
        "AND NOT EXISTS(SELECT 1 FROM agent_impersonations p WHERE p.agent_id=m.id AND p.status='active') "
        + ("FOR UPDATE OF m,w,o" if lock else ""),
        (
            source.agent_id,
            target.observation_id,
            source.work_id,
            source.machine,
            source.generation,
            source.owner,
        ),
    ).fetchone()
    if row is None or row[0] != target.model_dump(mode="json"):
        return False
    resources = decode_resources(row[2])
    return (
        row[3] == "settled"
        and row[4] is not None
        and managed_resources(row[1], source)
        and managed_resources(row[2], source)
        and isinstance(resources, IncarnationResources)
        and not resources.requests
        and history_matches(conn, target)
    )


def observe(pool: ConnectionPool, agent_id: int) -> CompactTarget:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT target FROM native_compact_observations WHERE agent_id=%s "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            (agent_id,),
        ).fetchall()
        if rows:
            target = CompactTarget.model_validate(rows[0][0])
            if eligible(conn, target, lock=False):
                return target
    raise CompactConflictError("no eligible compact producer observation")


def accept(
    pool: ConnectionPool, key: str, agent_id: int, target: CompactTarget
) -> CompactAcceptance:
    if target.source.agent_id != agent_id:
        raise CompactConflictError("compact target belongs to another agent")
    body = target.model_dump(mode="json")
    with write_transaction(pool) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
        row = conn.execute(
            "SELECT request,acceptance FROM native_compact_commands WHERE operation_key=%s", (key,)
        ).fetchone()
        if row is not None:
            if row[0] != body:
                raise CompactConflictError("compact key identifies a different source target")
            return CompactAcceptance.model_validate(row[1])
        if not eligible(conn, target, lock=True):
            raise CompactConflictError("observed compact source is no longer eligible")
        pending = conn.execute(
            "SELECT 1 FROM native_compact_commands WHERE agent_id=%s AND released_at IS NULL",
            (agent_id,),
        ).fetchone()
        cancel = conn.execute(
            "SELECT 1 FROM native_cancel_commands WHERE agent_id=%s AND outcome IN ('accepted','uncertain')",
            (agent_id,),
        ).fetchone()
        if pending is not None or cancel is not None:
            raise CompactConflictError("agent already has an unresolved native command")
        acceptance = CompactAcceptance(command_id=uuid4(), target=target)
        conn.execute(
            "INSERT INTO native_compact_commands(id,agent_id,operation_key,request,acceptance) "
            "VALUES (%s,%s,%s,%s,%s)",
            (
                acceptance.command_id,
                agent_id,
                key,
                Jsonb(body),
                Jsonb(acceptance.model_dump(mode="json")),
            ),
        )
        record_audit(
            conn,
            prepare_event_log(
                event_type="compact",
                agent_id=agent_id,
                source="user",
                payload={
                    "compact_kind": "request",
                    "command_id": str(acceptance.command_id),
                    "protocol": 1,
                },
            ),
        )
        return acceptance


def status(pool: ConnectionPool, agent_id: int, command_id: object) -> CompactStatus:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT acceptance,outcome,reason,checkpoint_id,recovery_checkpoint_id,attempt_id,execution,result IS NOT NULL,attempt_provider,released_at IS NOT NULL "
            "FROM native_compact_commands "
            "WHERE agent_id=%s AND id=%s",
            (agent_id, command_id),
        ).fetchone()
    if row is None:
        raise CompactConflictError("compact command is absent")
    return CompactStatus(
        acceptance=CompactAcceptance.model_validate(row[0]),
        outcome=CompactOutcome(row[1]),
        reason=row[2],
        checkpoint_id=row[3],
        recovery_checkpoint_id=row[4],
        attempt_id=row[5],
        attempt_provider=row[8],
        execution=None if row[6] is None else NativeWorkTarget.model_validate(row[6]),
        result_available=row[7],
        continuation_released=row[9],
    )
