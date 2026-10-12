"""Hosted incarnation admission and settlement, replacing status-only writes."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Never
from uuid import UUID, uuid4

import psutil
import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted_claim import claim_admission_row
from agent.ownership.hosted_cleanup import finish_termination
from base import telemetry
from base.agents.impersonation.replacement import end_for_replacement
from base.agents.incarnation.lifecycle_acceptance import (
    RECORD_APPLIED,
    terminate_kills_shell_sessions,
)
from base.agents.incarnation.resource_admission import admit_resources_async
from base.agents.incarnation.resources import (
    IncarnationResources,
    ResourceEvidenceError,
    ResourceProcess,
    decode_resources,
)
from base.agents.observation.evidence import AdmissionOutcome
from base.db import Database
from base.db.transaction import async_write_transaction
from base.deploy.maintenance import admission
from base.deploy.progress_timeout import AGENT_LEASE_TTL_S
from base.events.live.announce import publish_agent_updated
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources
from base.telemetry.audit_events import prepare_event_log, record_audit_async


class _HostedAdmissionRefusedError(Exception):
    """Roll back speculative resource transfer before returning a refusal."""

    def __init__(self, outcome: AdmissionOutcome, detail: str | None = None) -> None:
        self.outcome = outcome
        self.detail = detail


def _refuse_hosted_admission(
    outcome: AdmissionOutcome = AdmissionOutcome.ADMISSION_GUARD_REFUSED,
    detail: str | None = None,
) -> Never:
    raise _HostedAdmissionRefusedError(outcome, detail)


async def _record_admission_refusal(
    pool: AsyncConnectionPool,
    agent_id: int,
    machine: str,
    expected_from: str,
    attempt_at: datetime,
    outcome: AdmissionOutcome,
    detail: str | None = None,
) -> None:
    """Keep an older, delayed refusal from replacing a later successful claim; a
    detail is durable row state an operator must resolve, so it is logged loudly."""
    if detail is not None:
        logger.warning("agent {} admission refused ({}): {}", agent_id, outcome.value, detail)
    async with async_write_transaction(pool) as conn:
        await conn.execute(
            "UPDATE agents_meta SET last_admission_outcome=%s, "
            "last_admission_at=clock_timestamp(), "
            "last_launch_failure_reason=NULL, last_launch_failure_at=NULL "
            "WHERE id=%s AND machine=%s AND status=%s "
            "AND (last_admission_at IS NULL OR last_admission_at <= %s)",
            (outcome.value, agent_id, machine, expected_from, attempt_at),
        )


async def _admission_attempt_at(pool: AsyncConnectionPool) -> datetime:
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT clock_timestamp()")).fetchone()
    assert row is not None  # noqa: S101 — scalar SELECT always returns one row
    return row[0]


async def apply_hosted_lifecycle(
    pool: AsyncConnectionPool,
    incarnation: RuntimeIncarnation,
    *,
    bus: EventBus,
    kill_shell_sessions: Callable[[int, int], None] | None = None,
    expected_command_id: int | None = None,
    resources: HostedTurnResources | None,
) -> str | None:
    """Apply after the existing single-flight continuation has safely ended.

    The durable pointer, not a graph boolean or cache entry, identifies the
    command. An optional expected ID fences a completed invocation to its original
    command across database recovery. Restart releases ownership atomically, so any
    successor admission must create a new incarnation before observing it.
    Termination is observed in this same transaction: the caller has already
    returned from the real continuation and dropped its non-authoritative cache.

    When a terminate asked to take the agent's shell sessions with it, the
    row lock captures the next shell ID. After committing the termination,
    `kill_shell_sessions(agent_id, cutoff)` attempts only older sessions. The
    host reports cleanup failures; they cannot roll back the durable death.
    A crash or a session created after this cutoff can leave live sessions for operators.
    """
    from base.native_process.turn_identity import hosted_resources_settled

    if not hosted_resources_settled(resources):
        return None
    shell_cutoff: int | None = None
    async with async_write_transaction(pool) as conn:
        from base.agents.incarnation.resource_admission import require_resources_closed_async

        await require_resources_closed_async(conn, incarnation.agent_id)
        cursor = await conn.execute(
            "SELECT lifecycle_command_id,lease_expires_at,session_index FROM agents_meta WHERE id=%s "
            "AND runtime_generation=%s AND runtime_owner=%s AND runtime_kind='hosted' "
            "AND status IN ('running','idling') "
            "AND (%s::bigint IS NULL OR lifecycle_command_id=%s) FOR UPDATE",
            (
                incarnation.agent_id,
                incarnation.generation,
                incarnation.owner,
                expected_command_id,
                expected_command_id,
            ),
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
                "runtime_owner=NULL,runtime_kind=NULL,lease_expires_at=NULL WHERE id=%s",
                (incarnation.agent_id,),
            )
        elif lifecycle_kind == "terminate":
            if await terminate_kills_shell_sessions(conn, incarnation.agent_id):
                if kill_shell_sessions is None:
                    raise RuntimeError("shell-session kill requested but no killer is bound")
                shell_cutoff = row[2]
            await conn.execute(
                "UPDATE agents_meta SET status='terminated',termination_source='user',"
                "lease_expires_at=NULL WHERE id=%s",
                (incarnation.agent_id,),
            )
        else:
            raise ValueError(f"not an executable lifecycle command: {lifecycle_kind}")
        await conn.execute(RECORD_APPLIED, (row[0],))  # an applied restart is a release
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
        await finish_termination(bus, incarnation.agent_id, shell_cutoff, kill_shell_sessions)
    return lifecycle_kind


async def _held_owner_matches(pool: AsyncConnectionPool, agent_id: int, owner: UUID) -> bool:
    """A held unit may continue only the row this exact boot already owns.

    The pending-command check keeps non-cohort wakes out; the owner check is
    the successor fence — a crash that replaced the boot retains the hold and
    requires explicit cancellation or repair, never a fake ACK.
    """
    if admission.pending_command(agent_id) is None:
        return False
    async with pool.connection() as conn:
        held_owner = await (
            await conn.execute(
                "SELECT runtime_owner,runtime_kind FROM agents_meta WHERE id=%s", (agent_id,)
            )
        ).fetchone()
    return held_owner == (owner, "hosted")


async def _dead_predecessor_evidence(
    previous: tuple[Any, ...],
    *,
    owner: UUID,
    machine: str,
    host: psutil.Process,
) -> ResourceProcess | None:
    """The exited predecessor host process that licenses replacing this local row, if any.

    Runs inside the caller's metadata-lock transaction, so the exact-process
    observation is bound to the locked row state. A live exact host raises the
    refusal that rolls the transaction back. A row without stored host-process
    evidence (``incarnation_resources IS NULL``) offers no such proof: it keeps
    waiting for its lease to expire.
    """
    # Resolved at call time: the exact-exit probe is monkeypatched at its
    # source module in the resident tests.
    from base.agents.incarnation.exec_owner_recovery import process_ended

    if previous[1] == owner or previous[3] != machine or previous[4] is None:
        return None
    prior_resources = decode_resources(previous[4])
    if (
        isinstance(prior_resources, IncarnationResources)
        and not prior_resources.requests
        and prior_resources.frozen_by is None
        and prior_resources.host_process is not None
    ):
        # The row lock binds this monotonic exact-process observation to the
        # resource transfer in the same transaction.
        current_host = ResourceProcess.capture(host)
        if not prior_resources.host_process.same_birth(current_host):
            if not await asyncio.to_thread(process_ended, prior_resources.host_process):
                _refuse_hosted_admission()
            return prior_resources.host_process
        # The same real host may transfer a closed incarnation only through
        # admit_resources_async's durable predecessor check.
    return None


async def _lock_previous(conn: psycopg.AsyncConnection[Any], agent_id: int, owner: UUID) -> Any:
    """Lock and return the agent's runtime row; refuses an unknown row, a held cohort or
    stored evidence the current model cannot decode."""
    previous = await (
        await conn.execute(
            "SELECT runtime_generation,runtime_owner,runtime_kind,machine,"
            "incarnation_resources,lease_expires_at,native_work_id FROM agents_meta "
            "WHERE id=%s FOR UPDATE",
            (agent_id,),
        )
    ).fetchone()
    if previous is None:
        _refuse_hosted_admission()
    if admission.held() and (
        admission.pending_command(agent_id) is None or previous[1:3] != (owner, "hosted")
    ):
        # A successor cannot certify that its predecessor flushed.
        # A host crash during drain therefore retains the hold and
        # requires explicit cancellation/recovery, never a fake ACK.
        _refuse_hosted_admission()
    if previous[4] is not None:
        # A stored value the current model cannot decode (a retired
        # writer's shape) refuses under every admission.
        try:
            decode_resources(previous[4])
        except ResourceEvidenceError as exc:
            _refuse_hosted_admission(AdmissionOutcome.RESOURCE_FENCE, str(exc))
    return previous


async def admit_hosted_runtime(
    pool: AsyncConnectionPool,
    agent_id: int,
    machine: str,
    owner: UUID,
    *,
    db: Database,
    expected_from: str,
) -> RuntimeIncarnation | None:
    """Keep this owner's logical incarnation across turns; reject live others.

    A row owned by another host is replaced only after its lease expires, or
    earlier when its stored host process provably exited.

    Admission reads and locks no deployment-wide state.
    """
    from base.agents.incarnation.exec_owner_recovery import recover_local_resources
    from base.agents.recovery.breaker import admit_pending_wake

    if not await admit_pending_wake(pool, agent_id):
        return None
    attempt_at = await _admission_attempt_at(pool)
    if admission.held() and not await _held_owner_matches(pool, agent_id, owner):
        await _record_admission_refusal(
            pool, agent_id, machine, expected_from, attempt_at, AdmissionOutcome.MAINTENANCE_HOLD
        )
        return None

    await asyncio.to_thread(recover_local_resources, db, agent_id, machine)
    native = psutil.Process()
    host_identity = ResourceProcess.capture(native)
    recorded: list[telemetry.Event] = []
    try:
        async with async_write_transaction(pool) as conn:
            previous = await _lock_previous(conn, agent_id, owner)
            generation = (
                previous[0]
                if previous[1:3] == (owner, "hosted") and previous[0] is not None
                else uuid4()
            )
            exited_predecessor = await _dead_predecessor_evidence(
                previous, owner=owner, machine=machine, host=native
            )

            await admit_resources_async(
                conn,
                RuntimeIncarnation(agent_id, generation, owner),
                host_identity,
                exited_predecessor=exited_predecessor,
            )
            # Exact local host death and resource closure are stronger than
            # its remaining lease; the same row lock protects both proofs.
            row = await claim_admission_row(
                conn,
                agent_id=agent_id,
                machine=machine,
                owner=owner,
                generation=generation,
                expected_from=expected_from,
                takeover=exited_predecessor is not None,
            )
            if row is None:
                # Resource transfer and ordinary admission are one transaction.
                _refuse_hosted_admission()
            from agent.ownership.lifecycle_intent import observe_hosted_admission

            await observe_hosted_admission(conn, RuntimeIncarnation(agent_id, row[0], owner))
            ended_lease = await end_for_replacement(
                conn, agent_id, RuntimeIncarnation(agent_id, row[0], owner)
            )
            if ended_lease is not None:
                logger.info(
                    "impersonation lease closed by the replacement runtime's admission",
                    agent_id=agent_id,
                    lease_id=ended_lease,
                )
            admitted_event = prepare_event_log(
                event_type="status_change",
                agent_id=agent_id,
                source="system",
                payload={"from": expected_from, "to": "running"},
            )
            recorded.append(await record_audit_async(conn, admitted_event))
    except _HostedAdmissionRefusedError as exc:
        await _record_admission_refusal(
            pool, agent_id, machine, expected_from, attempt_at, exc.outcome, exc.detail
        )
        return None
    for event in recorded:
        telemetry.emit_prepared(event)
    logger.info(
        "hosted runtime admitted", agent_id=agent_id, generation=str(row[0]), owner=str(owner)
    )
    return RuntimeIncarnation(agent_id, row[0], owner)


async def settle_hosted_runtime(
    pool: AsyncConnectionPool,
    incarnation: RuntimeIncarnation,
    *,
    bus: EventBus,
    resources: HostedTurnResources | None,
) -> bool:
    """Settle an ordinary turn; only durable lifecycle apply releases ownership.

    The corpse marker `last_turn_fatal_at` is deliberately untouched here:
    a completed LLM turn already cleared it atomically (`_persist_last_active`
    writes `last_active_at` and clears the marker in one UPDATE — the only
    `last_active_at` writer), while a no-work park (crash-fresh halted claim
    or an open circuit breaker) leaves it for the reaper. Writing it here would
    only relabel a crash-dead row healthy and resume its lease renewal forever.
    """
    from base.native_process.turn_identity import hosted_resources_settled

    if not hosted_resources_settled(resources):
        return False
    async with async_write_transaction(pool) as conn:
        cur = await conn.execute(
            "UPDATE agents_meta SET status = 'idling' "
            "WHERE id = %s AND status = 'running' AND runtime_kind = 'hosted' "
            "AND runtime_generation = %s AND runtime_owner = %s",
            (
                incarnation.agent_id,
                incarnation.generation,
                incarnation.owner,
            ),
        )
        changed = cur.rowcount == 1
        settled_event = (
            await record_audit_async(
                conn,
                prepare_event_log(
                    event_type="status_change",
                    agent_id=incarnation.agent_id,
                    source="system",
                    payload={"from": "running", "to": "idling"},
                ),
            )
            if changed
            else None
        )
    if settled_event is not None:
        telemetry.emit_prepared(settled_event)
    if changed:
        await publish_agent_updated(bus, incarnation.agent_id)
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


@dataclass(frozen=True)
class TurnFatalStamp:
    """One crash's stamp outcome: `applied` is the CAS result."""

    applied: bool


async def stamp_turn_fatal(
    pool: AsyncConnectionPool, incarnation: RuntimeIncarnation
) -> TurnFatalStamp:
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
            "AND runtime_generation = %s AND runtime_owner = %s RETURNING id",
            (incarnation.agent_id, incarnation.generation, incarnation.owner),
        )
        applied = await cur.fetchone() is not None
    if applied:
        logger.info(
            "hosted turn crashed — corpse marked for the reaper",
            event="host_turn_corpse_marked",
            agent_id=incarnation.agent_id,
        )
    return TurnFatalStamp(applied=applied)


@dataclass(frozen=True)
class TurnSettlement:
    """The settle boundary's facts about one closed turn: `stamp` is the crash
    stamp (None when the turn did not crash or its stamp failed), and `settled`
    says the settle wrote the row idling, gated on fully discharged turn
    resources."""

    stamp: TurnFatalStamp | None
    settled: bool


async def settle_and_stamp_turn(
    pool: AsyncConnectionPool,
    incarnation: RuntimeIncarnation,
    *,
    bus: EventBus,
    exited: bool,
    crashed: bool,
    resources: HostedTurnResources | None,
) -> TurnSettlement:
    """Close one hosted turn: stamp the corpse marker first, then settle.

    The stamp is best-effort and independent of settlement (which may be
    blocked by unsettled resources): the death evidence must land even when
    the idling write could not. It must also run BEFORE the settle — its CAS
    matches `status='running'`, so a settled row would refuse the stamp.

    Returns both halves to the settlement boundary: the crash stamp and
    whether the settle reached idling.
    """
    stamp: TurnFatalStamp | None = None
    if crashed:
        try:
            stamp = await stamp_turn_fatal(pool, incarnation)
        except Exception:
            logger.opt(exception=True).warning(
                "corpse marker stamp failed — the row stays alive-"
                "looking until a later stamp or a completed turn",
                event="corpse_stamp_failed",
                agent_id=incarnation.agent_id,
            )
    settled = False
    if not exited:
        settled = await settle_hosted_runtime(pool, incarnation, bus=bus, resources=resources)
    return TurnSettlement(stamp=stamp, settled=settled)


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
