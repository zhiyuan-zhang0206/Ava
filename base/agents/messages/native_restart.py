"""One immutable guarded restart acceptance, bound by the lifecycle writer."""

import hashlib
import json
from collections.abc import Callable

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.incarnation.lifecycle_acceptance import accept_lifecycle_command
from base.agents.incarnation.native_restart_models import (
    NativeRestartAcceptance,
    NativeRestartOutcome,
    NativeRestartProgress,
    NativeRestartReason,
    NativeRestartRequest,
)
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.messages.native_cancel import observe_native_work_in_transaction
from base.db import insert_inbound_message_in_transaction
from base.db.transaction import write_transaction
from base.lm.model_config import validate_restart_model_config
from base.native_process.runtime_incarnation import RuntimeIncarnation


class NativeRestartConflictError(ValueError):
    """Original restart identity conflicts or the observed target is ineligible."""


def lookup_native_restart(
    pool: ConnectionPool, key: str, agent_id: int, request: NativeRestartRequest
) -> NativeRestartAcceptance | None:
    """Gateway replay precedes mutable home-machine routing or source lookup."""
    if agent_id != request.target.agent_id:
        raise NativeRestartConflictError("native restart belongs to another agent")
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT request_hash,acceptance FROM native_restart_commands WHERE operation_key=%s",
            (key,),
        ).fetchone()
    if row is None:
        return None
    if row[0] != _request_hash(request):
        raise NativeRestartConflictError("native restart key was used for another request")
    return NativeRestartAcceptance.model_validate(row[1])


def accept_native_restart(
    pool: ConnectionPool,
    key: str,
    agent_id: int,
    request: NativeRestartRequest,
    prepare_overlay: Callable[[NativeRestartRequest], dict[str, object] | None],
) -> NativeRestartAcceptance:
    """Receipt/source/overlay commit together; replay never prepares new config."""
    if agent_id != request.target.agent_id:
        raise NativeRestartConflictError("native restart belongs to another agent")
    payload = request.model_dump(mode="json")
    request_hash = _request_hash(request)
    with write_transaction(pool) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
        previous = conn.execute(
            "SELECT request_hash,acceptance FROM native_restart_commands WHERE operation_key=%s",
            (key,),
        ).fetchone()
        if previous is not None:
            if previous[0] != request_hash:
                raise NativeRestartConflictError("native restart key was used for another request")
            return NativeRestartAcceptance.model_validate(previous[1])
        _require_fresh_target(conn, agent_id, request.target)
        overlay = prepare_overlay(request)
        if overlay:
            with conn.cursor() as cur:
                validate_restart_model_config(cur, agent_id, overlay)
            conn.execute(
                "UPDATE agents_meta SET config_overlay=COALESCE(config_overlay,'{}'::jsonb)||%s WHERE id=%s",
                (Jsonb(overlay), agent_id),
            )
        with conn.cursor() as cursor:
            source_id, event = insert_inbound_message_in_transaction(
                cursor,
                agent_id,
                "",
                request.source,
                kind="restart",
                payload={"config_overlay": overlay} if overlay else None,
            )
        target = request.target
        intent = accept_lifecycle_command(
            conn,
            RuntimeIncarnation(agent_id=agent_id, generation=target.generation, owner=target.owner),
        )
        if intent is None or intent.id != source_id:
            raise NativeRestartConflictError("native restart did not bind its original command")
        acceptance = NativeRestartAcceptance(
            command_id=intent.id, target=target, config_overlay=overlay
        )
        conn.execute(
            "INSERT INTO native_restart_commands(operation_key,command_id,agent_id,work_id,"
            "target_generation,target_owner,request_hash,request,acceptance) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                key,
                intent.id,
                agent_id,
                target.work_id,
                target.generation,
                target.owner,
                request_hash,
                Jsonb(payload),
                Jsonb(acceptance.model_dump(mode="json")),
            ),
        )
    if event is not None:
        from base import telemetry

        telemetry.emit_prepared(event)
    return acceptance


def _request_hash(request: NativeRestartRequest) -> str:
    """Compare immutable raw JSON, preserving boolean and numeric representations."""
    encoded = json.dumps(
        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _require_fresh_target(
    conn: psycopg.Connection, agent_id: int, target: NativeWorkTarget
) -> None:
    row = conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,)
    ).fetchone()
    if row is None or row[0] is not None:
        raise NativeRestartConflictError("native restart has an unfinished lifecycle owner")
    if observe_native_work_in_transaction(conn, agent_id) != target:
        raise NativeRestartConflictError("observed ACTIVE native work is not eligible")
    conn.execute("SELECT id FROM native_graph_work WHERE id=%s FOR UPDATE", (target.work_id,))
    if (
        conn.execute(
            "SELECT 1 FROM inbound_messages i JOIN agents_meta m ON m.id=i.agent_id "
            "WHERE i.agent_id=%s AND i.kind IN ('restart','terminate') AND i.status IN ('pending','claimed') "
            "AND i.applied_at IS NULL AND i.id>COALESCE(m.last_resurrect_inbound_id,0)",
            (agent_id,),
        ).fetchone()
        or conn.execute(
            "SELECT 1 FROM native_restart_commands WHERE work_id=%s", (target.work_id,)
        ).fetchone()
    ):
        raise NativeRestartConflictError("native restart already has a competing command")


def native_restart_progress(
    pool: ConnectionPool, agent_id: int, command_id: int
) -> NativeRestartProgress | None:
    """Read retained proof; source disappearance is uncertain, never another effect."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT acceptance,outcome,applied_at,observed_at,outcome_reason,"
            "EXISTS(SELECT 1 FROM inbound_messages WHERE id=r.command_id) "
            "FROM native_restart_commands r WHERE agent_id=%s AND command_id=%s",
            (agent_id, command_id),
        ).fetchone()
    if row is None:
        return None
    outcome, reason = row[1], NativeRestartReason(row[4]) if row[4] is not None else None
    if outcome == NativeRestartOutcome.ACCEPTED and not row[5]:
        outcome, reason = (
            NativeRestartOutcome.UNCERTAIN,
            NativeRestartReason.SOURCE_COMMAND_UNAVAILABLE,
        )
    return NativeRestartProgress(
        acceptance=NativeRestartAcceptance.model_validate(row[0]),
        outcome=outcome,
        applied_at=row[2],
        observed_at=row[3],
        reason=reason,
    )


async def original_guarded_restart_id(
    conn: psycopg.AsyncConnection, target: NativeWorkTarget
) -> int | None:
    row = await (
        await conn.execute(
            "SELECT command_id,acceptance FROM native_restart_commands WHERE work_id=%s",
            (target.work_id,),
        )
    ).fetchone()
    if row is None:
        return None
    accepted = NativeRestartAcceptance.model_validate(row[1])
    if accepted.target != target or accepted.command_id != row[0]:
        raise NativeRestartConflictError("guarded restart original work identity differs")
    return row[0]


async def completed_guarded_restart(
    conn: psycopg.AsyncConnection, incarnation: RuntimeIncarnation, command_id: int
) -> bool:
    row = await (
        await conn.execute(
            "SELECT acceptance FROM native_restart_commands WHERE command_id=%s AND agent_id=%s "
            "AND target_generation=%s AND target_owner=%s AND applied_at IS NOT NULL "
            "AND outcome IN ('applied','observed')",
            (command_id, incarnation.agent_id, incarnation.generation, incarnation.owner),
        )
    ).fetchone()
    if row is None:
        return False
    acceptance = NativeRestartAcceptance.model_validate(row[0])
    return acceptance.command_id == command_id and (
        acceptance.target.agent_id,
        acceptance.target.generation,
        acceptance.target.owner,
    ) == (incarnation.agent_id, incarnation.generation, incarnation.owner)


async def superseded_guarded_restart(
    conn: psycopg.AsyncConnection, target: NativeWorkTarget, command_id: int
) -> bool:
    """Read canonical original no-effect proof, never applied execution."""
    row = await (
        await conn.execute(
            "SELECT acceptance,outcome,applied_at,observed_at,outcome_reason "
            "FROM native_restart_commands WHERE command_id=%s AND agent_id=%s",
            (command_id, target.agent_id),
        )
    ).fetchone()
    if row is None:
        return False
    proof = NativeRestartProgress(
        acceptance=NativeRestartAcceptance.model_validate(row[0]),
        outcome=row[1],
        applied_at=row[2],
        observed_at=row[3],
        reason=row[4],
    )
    if proof.acceptance.target != target or proof.acceptance.command_id != command_id:
        raise NativeRestartConflictError("guarded restart original work identity differs")
    return proof.outcome == NativeRestartOutcome.SUPERSEDED and proof.reason in (
        NativeRestartReason.TARGET_REPLACED,
        NativeRestartReason.RESURRECT,
        NativeRestartReason.FORCE_TERMINATE,
    )
