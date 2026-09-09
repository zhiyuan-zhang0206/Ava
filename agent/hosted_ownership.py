"""Hosted incarnation admission and settlement, replacing status-only writes."""

import asyncio
from typing import Never
from uuid import UUID, uuid4

import psutil
from psycopg_pool import AsyncConnectionPool

from shared import maintenance
from shared.audit_events import insert_event_log_async
from shared.db_transaction import async_write_transaction
from shared.deploy_timing import AGENT_LEASE_TTL_S, CORPSE_REAP_GRACE_S
from shared.live_announce import publish_agent_updated
from shared.log import logger
from shared.runtime_admission import (
    PublicationAdmissionDeferredError,
    RuntimeAdmission,
    process_runtime_admission,
    require_current_for_managed,
)
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
    publication: RuntimeAdmission | None = None,
) -> RuntimeIncarnation | None:
    """Keep this owner's logical incarnation across turns; reject live others."""
    from shared.exec_owner_recovery import recover_local_resources

    if maintenance.held():
        if maintenance.pending_command(agent_id) is None:
            return None
        async with pool.connection() as conn:
            held_owner = await (
                await conn.execute(
                    "SELECT runtime_owner,runtime_kind FROM agents_meta WHERE id=%s",
                    (agent_id,),
                )
            ).fetchone()
        if held_owner != (owner, "hosted"):
            return None

    await asyncio.to_thread(recover_local_resources, agent_id, machine)
    native = psutil.Process()
    from shared.exec_owner_recovery import process_ended
    from shared.incarnation_resources import (
        IncarnationResources,
        ResourceEvidenceError,
        ResourceProcess,
        decode_resources,
    )
    from shared.resource_admission import admit_resources_async

    host_identity = ResourceProcess(pid=native.pid, birth=native.create_time())
    if publication is None:
        publication = await asyncio.to_thread(process_runtime_admission)
    else:
        await asyncio.to_thread(publication.revalidate)
    try:
        async with async_write_transaction(pool) as conn:
            try:
                publication_decision = await publication.decide_async(conn)
            except PublicationAdmissionDeferredError:
                return None
            previous = await (
                await conn.execute(
                    "SELECT runtime_generation,runtime_owner,runtime_kind,machine,"
                    "incarnation_resources FROM agents_meta WHERE id=%s FOR UPDATE",
                    (agent_id,),
                )
            ).fetchone()
            if previous is None:
                _refuse_hosted_admission()
            if maintenance.held() and (
                maintenance.pending_command(agent_id) is None or previous[1:3] != (owner, "hosted")
            ):
                # A successor cannot certify that its predecessor flushed.
                # A host crash during drain therefore retains the hold and
                # requires explicit cancellation/recovery, never a fake ACK.
                _refuse_hosted_admission()
            try:
                require_current_for_managed(publication_decision, previous[4])
            except ResourceEvidenceError:
                return None
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
                    current_host = ResourceProcess(pid=native.pid, birth=native.create_time())
                    if prior_resources.host_process != current_host:
                        if not await asyncio.to_thread(process_ended, prior_resources.host_process):
                            _refuse_hosted_admission()
                        exited_predecessor = prior_resources.host_process
                    # The same real host may transfer a closed incarnation only
                    # through admit_resources_async's durable predecessor check.

            await admit_resources_async(
                conn,
                RuntimeIncarnation(agent_id, generation, owner),
                host_identity,
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
    """Settle an ordinary turn; only durable lifecycle apply releases ownership.

    The corpse marker `last_turn_fatal_at` is deliberately untouched here:
    a completed LLM turn already cleared it atomically (`_persist_last_active`
    writes `last_active_at` and clears the marker in one UPDATE — the only
    `last_active_at` writer), while a no-work park (crash-fresh halted claim
    or an open circuit breaker) leaves it for the reaper. Writing it here would
    only relabel a crash-dead row healthy and resume its lease renewal forever.
    """
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
    """Renew idle and busy responsibility from the existing host liveness beat.

    Crash-marked rows (`last_turn_fatal_at` set) are corpses awaiting the
    reaper — their lease must NOT be renewed, so liveness decays offline and
    the frontend stops rendering them alive while the grace window runs.
    """
    async with async_write_transaction(pool) as conn:
        await conn.execute(
            "UPDATE agents_meta SET lease_expires_at = now() + make_interval(secs => %s) "
            "WHERE machine = %s AND runtime_kind = 'hosted' AND runtime_owner = %s "
            "AND runtime_generation IS NOT NULL AND status IN ('running', 'idling') "
            "AND last_turn_fatal_at IS NULL",
            (AGENT_LEASE_TTL_S, machine, owner),
        )


async def stamp_turn_fatal(pool: AsyncConnectionPool, incarnation: RuntimeIncarnation) -> bool:
    """Mark this incarnation's row crash-dead after the hosted runner saw the
    turn die (fatal LLM class or an unclassified exception).

    COALESCE keeps the FIRST death since the last completed LLM turn: a corpse
    re-woken by heartbeats keeps re-crashing, and re-stamping each time would
    restart the reap grace forever — the reaper would never fire. Recovery
    clears the mark (a completed LLM turn, or the resurrect transition), so the
    next crash re-arms it fresh.

    Best-effort and settle-independent: a settlement blocked by unsettled
    resources must not lose the death evidence (the 5858 corpse settled 3m52s
    after its last turn ended; the stamp landed regardless).
    """
    async with async_write_transaction(pool) as conn:
        cur = await conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = "
            "COALESCE(last_turn_fatal_at, clock_timestamp()) "
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
        logger.info(
            "hosted turn crashed — corpse marked for the reaper",
            event="host_turn_corpse_marked",
            agent_id=incarnation.agent_id,
        )
    return changed


async def reap_crash_corpses(
    pool: AsyncConnectionPool,
    machine: str,
    owner: UUID,
) -> list[int]:
    """Terminate crash-marked idling corpses whose grace window has elapsed.

    The positive death signal is the row's own `last_turn_fatal_at` (stamped
    firsthand by this host when the turn died), never staleness alone — an
    unmarked idling row is a live agent and is never touched. Idling-only:
    a `running` row has a live invocation (or claim park) in flight. The
    incarnation CAS already happened at stamp time, so the mark names exactly
    the generation that died; a row woken in the grace window either completes
    a turn (clears the mark) or parks and keeps it.

    Returns the reaped agent ids (the caller publishes their snapshots).

    Owner scope is lease-qualified, not owner-only: a host restart mints a
    fresh owner UUID, so a corpse marked under a predecessor instance would
    otherwise hang offline forever (never adopted, never reaped). A row whose
    lease is live is protected — it belongs to some live host's beat — while
    an ownerless or lease-expired row is dead-or-abandoned and fair game.
    """
    async with async_write_transaction(pool) as conn:
        rows = await (
            await conn.execute(
                "UPDATE agents_meta SET status = 'terminated', "
                "termination_source = 'reaper', lease_expires_at = NULL, "
                "runtime_protocol_version = 0 "
                "WHERE machine = %s AND runtime_kind = 'hosted' AND status = 'idling' "
                "AND last_turn_fatal_at IS NOT NULL "
                "AND last_turn_fatal_at <= now() - make_interval(secs => %s) "
                "AND (runtime_owner = %s OR lease_expires_at IS NULL "
                "OR lease_expires_at <= now()) "
                "RETURNING id",
                (machine, CORPSE_REAP_GRACE_S, owner),
            )
        ).fetchall()
        for (agent_id,) in rows:
            await insert_event_log_async(
                event_type="status_change",
                agent_id=agent_id,
                source="system",
                payload={"from": "idling", "to": "terminated", "reason": "corpse_reaper"},
            )
    reaped = [row[0] for row in rows]
    if reaped:
        logger.info(
            "corpse reaper: terminated {n} crash-dead row(s)",
            event="corpse_reaper_terminated",
            n=len(reaped),
        )
    for agent_id in reaped:
        # Best-effort by design: the durable flip already committed; the
        # announce only refreshes mounted frontends.
        try:
            await publish_agent_updated(pool, agent_id)
        except Exception:
            logger.exception(
                "corpse reap snapshot publish failed",
                event="corpse_reaper_publish_failed",
                agent_id=agent_id,
            )
    return reaped


async def settle_and_stamp_turn(
    pool: AsyncConnectionPool,
    incarnation: RuntimeIncarnation,
    *,
    exited: bool,
    crashed: bool,
) -> None:
    """Close one hosted turn: stamp the corpse marker first, then settle.

    The stamp is best-effort and independent of settlement (which may be
    blocked by unsettled resources): the death evidence must land even when
    the idling write could not. It must also run BEFORE the settle — its CAS
    matches `status='running'`, so a settled row would refuse the stamp.
    """
    if crashed:
        try:
            await stamp_turn_fatal(pool, incarnation)
        except Exception:
            logger.warning(
                "corpse marker stamp failed — the row stays alive-"
                "looking until a later stamp or a completed turn",
                event="corpse_stamp_failed",
                agent_id=incarnation.agent_id,
                exc_info=True,
            )
    if not exited:
        await settle_hosted_runtime(pool, incarnation)


async def settle_stale_running_rows(pool: AsyncConnectionPool, machine: str) -> list[int]:
    """Restore rows left running by a previous hosted-runner instance.

    A pidless row may belong to another live host instance. Only unknown or
    expired hosted ownership licenses this atomic startup status settlement.
    """
    async with async_write_transaction(pool) as conn:
        rows = await (
            await conn.execute(
                "UPDATE agents_meta SET status = 'idling' "
                "WHERE status = 'running' AND pid IS NULL AND machine = %s "
                "AND (runtime_kind IS NULL OR runtime_kind = 'hosted') "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= now()) RETURNING id",
                (machine,),
            )
        ).fetchall()
    settled = [row[0] for row in rows]
    logger.info(
        "hosted stale-running settle: settled {n} row(s)",
        event="host_stale_running_settled",
        n=len(settled),
    )
    return settled
