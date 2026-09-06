"""Hosted incarnation admission and settlement, replacing status-only writes."""

import asyncio
from typing import Never
from uuid import UUID, uuid4

import psutil
from psycopg_pool import AsyncConnectionPool

from shared.audit_events import insert_event_log_async
from shared.db_transaction import async_write_transaction
from shared.deploy_timing import AGENT_LEASE_TTL_S
from shared.live_announce import publish_agent_updated
from shared.log import logger
from shared.runtime_incarnation import RuntimeIncarnation


class _HostedAdmissionRefusedError(Exception):
    """Roll back speculative resource transfer before returning a refusal."""


def _refuse_hosted_admission() -> Never:
    raise _HostedAdmissionRefusedError


async def apply_hosted_lifecycle(
    pool: AsyncConnectionPool, incarnation: RuntimeIncarnation
) -> str | None:
    """Apply after the existing single-flight continuation has safely ended.

    The durable pointer, not a graph boolean or cache entry, identifies the
    command. Restart releases ownership atomically with its decision, so any
    successor admission must create a new incarnation before observing it.
    Termination is observed in this same transaction: the caller has already
    returned from the real continuation and dropped its non-authoritative cache.
    """
    from shared.turn_identity import hosted_resources_settled

    if not hosted_resources_settled():
        return None
    async with async_write_transaction(pool) as conn:
        from shared.resource_admission import require_resources_closed_async

        await require_resources_closed_async(conn, incarnation.agent_id)
        cursor = await conn.execute(
            "SELECT lifecycle_command_id,lease_expires_at FROM agents_meta WHERE id=%s "
            "AND runtime_generation=%s AND runtime_owner=%s AND runtime_kind='hosted' "
            "AND status IN ('running','idling') FOR UPDATE",
            (incarnation.agent_id, incarnation.generation, incarnation.owner),
        )
        row = await cursor.fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        cursor = await conn.execute("SELECT %s > clock_timestamp()", (row[1],))
        if await cursor.fetchone() != (True,):
            return None
        cursor = await conn.execute(
            "SELECT kind FROM inbound_messages WHERE id=%s AND agent_id=%s "
            "AND target_generation=%s AND target_owner=%s AND status='claimed' "
            "AND applied_at IS NULL FOR UPDATE",
            (row[0], incarnation.agent_id, incarnation.generation, incarnation.owner),
        )
        command = await cursor.fetchone()
        if command is None:
            return None
        lifecycle_kind = command[0]
        if lifecycle_kind == "restart":
            await conn.execute(
                "UPDATE agents_meta SET status='idling',runtime_generation=NULL,"
                "runtime_owner=NULL,runtime_kind=NULL,lease_expires_at=NULL,"
                "runtime_protocol_version=0 WHERE id=%s",
                (incarnation.agent_id,),
            )
        elif lifecycle_kind == "terminate":
            await conn.execute(
                "UPDATE agents_meta SET status='terminated',termination_source='user',"
                "lease_expires_at=NULL,runtime_protocol_version=0 WHERE id=%s",
                (incarnation.agent_id,),
            )
        else:
            raise ValueError(f"not an executable lifecycle command: {lifecycle_kind}")
        await conn.execute(
            "UPDATE inbound_messages SET applied_at=clock_timestamp() WHERE id=%s", (row[0],)
        )
        if lifecycle_kind == "terminate":
            await conn.execute(
                "UPDATE inbound_messages SET observed_at=clock_timestamp(),status='done' "
                "WHERE id=%s",
                (row[0],),
            )
            await conn.execute(
                "UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s "
                "AND lifecycle_command_id=%s",
                (incarnation.agent_id, row[0]),
            )
    if lifecycle_kind == "terminate":
        await publish_agent_updated(pool, incarnation.agent_id)
    return lifecycle_kind


async def admit_hosted_runtime(
    pool: AsyncConnectionPool,
    agent_id: int,
    machine: str,
    owner: UUID,
    *,
    expected_from: str,
) -> RuntimeIncarnation | None:
    """Keep this owner's logical incarnation across turns; reject live others."""
    from shared.exec_owner_recovery import recover_local_resources

    await asyncio.to_thread(recover_local_resources, agent_id, machine)
    native = psutil.Process()
    from shared.exec_owner_recovery import process_ended
    from shared.incarnation_resources import (
        IncarnationResources,
        ResourceProcess,
        decode_resources,
    )
    from shared.resource_admission import admit_resources_async

    try:
        async with async_write_transaction(pool) as conn:
            previous = await (
                await conn.execute(
                    "SELECT runtime_generation,runtime_owner,runtime_kind,machine,"
                    "incarnation_resources FROM agents_meta WHERE id=%s FOR UPDATE",
                    (agent_id,),
                )
            ).fetchone()
            if previous is None:
                _refuse_hosted_admission()
            generation = (
                previous[0]
                if previous[1:3] == (owner, "hosted") and previous[0] is not None
                else uuid4()
            )
            exited_predecessor = None
            if previous[1] != owner and previous[3] == machine and previous[4] is not None:
                prior_resources = decode_resources(previous[4])
                if (
                    isinstance(prior_resources, IncarnationResources)
                    and not prior_resources.requests
                    and prior_resources.frozen_by is None
                    and prior_resources.host_process is not None
                ):
                    # The row lock binds this monotonic exact-process observation
                    # to the resource transfer in the same transaction.
                    if not await asyncio.to_thread(process_ended, prior_resources.host_process):
                        _refuse_hosted_admission()
                    exited_predecessor = prior_resources.host_process
            await admit_resources_async(
                conn,
                RuntimeIncarnation(agent_id, generation, owner),
                ResourceProcess(pid=native.pid, birth=native.create_time()),
                exited_predecessor=exited_predecessor,
            )
            row = await (
                await conn.execute(
                    "UPDATE agents_meta SET status = 'running', runtime_kind = 'hosted', "
                    "runtime_generation = CASE WHEN runtime_owner = %s AND runtime_kind = 'hosted' "
                    "AND runtime_generation IS NOT NULL "
                    "THEN runtime_generation ELSE %s END, runtime_owner = %s, "
                    "runtime_protocol_version = 0, "
                    "lease_expires_at = now() + make_interval(secs => %s) "
                    "WHERE id = %s AND machine = %s AND status = %s AND pid IS NULL "
                    "AND status IN ('running','idling') "
                    "AND NOT EXISTS (SELECT 1 FROM inbound_messages force "
                    "WHERE force.id=agents_meta.lifecycle_command_id AND force.kind='terminate' "
                    "AND force.status='claimed' AND force.applied_at IS NOT NULL "
                    "AND force.observed_at IS NULL) "
                    "AND (runtime_kind IS NULL OR runtime_kind = 'hosted') "
                    "AND (runtime_owner IS NULL OR runtime_owner = %s "
                    "OR lease_expires_at IS NULL OR lease_expires_at <= now()) "
                    "RETURNING runtime_generation",
                    (
                        owner,
                        generation,
                        owner,
                        AGENT_LEASE_TTL_S,
                        agent_id,
                        machine,
                        expected_from,
                        owner,
                    ),
                )
            ).fetchone()
            if row is None:
                # Resource transfer and ordinary admission are one transaction.
                _refuse_hosted_admission()
            from agent.lifecycle_observe import observe_hosted_admission

            await observe_hosted_admission(conn, RuntimeIncarnation(agent_id, row[0], owner))
            await insert_event_log_async(
                event_type="status_change",
                agent_id=agent_id,
                source="system",
                payload={"from": expected_from, "to": "running"},
            )
    except _HostedAdmissionRefusedError:
        return None
    logger.info(
        "hosted runtime admitted", agent_id=agent_id, generation=str(row[0]), owner=str(owner)
    )
    return RuntimeIncarnation(agent_id, row[0], owner)


async def settle_hosted_runtime(
    pool: AsyncConnectionPool,
    incarnation: RuntimeIncarnation,
) -> bool:
    """Settle an ordinary turn; only durable lifecycle apply releases ownership."""
    from shared.turn_identity import hosted_resources_settled

    if not hosted_resources_settled():
        return False
    async with async_write_transaction(pool) as conn:
        cur = await conn.execute(
            "UPDATE agents_meta SET status = 'idling', "
            "runtime_protocol_version = 0 "
            "WHERE id = %s AND status = 'running' AND runtime_kind = 'hosted' "
            "AND runtime_generation = %s AND runtime_owner = %s",
            (
                incarnation.agent_id,
                incarnation.generation,
                incarnation.owner,
            ),
        )
        changed = cur.rowcount == 1
        if changed:
            await insert_event_log_async(
                event_type="status_change",
                agent_id=incarnation.agent_id,
                source="system",
                payload={"from": "running", "to": "idling"},
            )
    if changed:
        await publish_agent_updated(pool, incarnation.agent_id)
    return changed


async def release_hosted_owner(
    pool: AsyncConnectionPool,
    machine: str,
    owner: UUID,
    in_flight: set[int],
) -> None:
    """Only settled tasks can release responsibility before host process exit."""
    async with async_write_transaction(pool) as conn:
        await conn.execute(
            "UPDATE agents_meta SET lease_expires_at = NULL "
            "WHERE machine = %s AND runtime_kind = 'hosted' AND runtime_owner = %s "
            "AND NOT (id = ANY(%s))",
            (machine, owner, list(in_flight)),
        )


async def renew_hosted_owner(pool: AsyncConnectionPool, machine: str, owner: UUID) -> None:
    """Renew idle and busy responsibility from the existing host liveness beat."""
    async with async_write_transaction(pool) as conn:
        await conn.execute(
            "UPDATE agents_meta SET lease_expires_at = now() + make_interval(secs => %s) "
            "WHERE machine = %s AND runtime_kind = 'hosted' AND runtime_owner = %s "
            "AND runtime_generation IS NOT NULL AND status IN ('running', 'idling')",
            (AGENT_LEASE_TTL_S, machine, owner),
        )
