"""Hosted incarnation admission and settlement, replacing status-only writes."""

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Never
from uuid import UUID, uuid4

import psutil
import psycopg
from psycopg_pool import AsyncConnectionPool

from shared import maintenance
from shared.audit_events import insert_event_log_async
from shared.db_transaction import async_write_transaction
from shared.deploy_timing import (
    AGENT_LEASE_TTL_S,
    CORPSE_REAP_GRACE_S,
    LEGACY_HOST_ADOPTION_SILENCE_S,
)
from shared.host_process_evidence import local_host_evidence
from shared.incarnation_resources import (
    IncarnationResources,
    ResourceEvidenceError,
    ResourceProcess,
    decode_resources,
)
from shared.live_announce import publish_agent_updated
from shared.log import logger
from shared.paths import ava_home
from shared.proc_tree import stable_create_time
from shared.resource_admission import admit_resources_async
from shared.runtime_admission import (
    AdmissionDecision,
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


async def align_accepting_binding(
    conn: psycopg.AsyncConnection[Any],
    agent_id: int,
    incarnation: RuntimeIncarnation,
) -> None:
    """Align an open lease's accepting-incarnation binding with the admitted
    incarnation, in the admission transaction (issue #2052).

    The lazy inheritance in ``shared.impersonation.native_status`` runs only at
    the replacement's first held wake. A lease released or expired between a
    hosted restart and that wake still fires
    ``restore_native_impersonation_owner``, which writes the recorded
    ``accepted_*`` snapshot back into ``agents_meta`` — the dead pre-restart
    incarnation, breaking every ``require_native`` check of the live process
    (task #2635's manual DB alignment was the same class of damage). Aligning
    here closes the window: by the time the new incarnation is visible, the
    trigger's write-back is already a no-op.

    Mirrors ``native_status``'s two branches. Legitimacy is the admission
    itself: the ``agents_meta`` row lock serializes this writer against the
    lazy sync (``require_native`` takes the same lock), and a lingering
    predecessor dies at its own row check before it can touch ``accepted_*``.
    """
    cursor = await conn.execute(
        "SELECT id,status,accepted_generation,accepted_owner FROM agent_impersonations "
        "WHERE agent_id=%s AND status IN ('requested','accepted','active') "
        "ORDER BY created_at LIMIT 1 FOR UPDATE",
        (agent_id,),
    )
    lease = await cursor.fetchone()
    if lease is None:
        return
    lease_id, status, accepted_generation, accepted_owner = lease
    if status == "accepted" and (accepted_generation, accepted_owner) != (
        incarnation.generation,
        incarnation.owner,
    ):
        # A crash before the checkpoint ACK never transfers control: ask the
        # replacement to decide again from its saved state, exactly as the
        # lazy native_status path would at its first wake.
        await conn.execute(
            "UPDATE agent_impersonations SET status='requested',accepted_generation=NULL,"
            "accepted_owner=NULL,consent_version=consent_version+1 WHERE id=%s",
            (lease_id,),
        )
        logger.info(
            "accepted lease binding reset for the replacement runtime",
            agent_id=agent_id,
            lease_id=str(lease_id),
        )
    elif status == "active" and (accepted_generation, accepted_owner) != (
        incarnation.generation,
        incarnation.owner,
    ):
        await conn.execute(
            "UPDATE agent_impersonations SET accepted_generation=%s,accepted_owner=%s WHERE id=%s",
            (incarnation.generation, incarnation.owner, lease_id),
        )
        logger.info(
            "active lease accepting-incarnation binding aligned at admission",
            agent_id=agent_id,
            lease_id=str(lease_id),
            generation=str(incarnation.generation),
        )


async def _held_owner_matches(pool: AsyncConnectionPool, agent_id: int, owner: UUID) -> bool:
    """A held unit may continue only the row this exact boot already owns.

    The pending-command check keeps non-cohort wakes out; the owner check is
    the successor fence — a crash that replaced the boot retains the hold and
    requires explicit cancellation or repair, never a fake ACK.
    """
    if maintenance.pending_command(agent_id) is None:
        return False
    async with pool.connection() as conn:
        held_owner = await (
            await conn.execute(
                "SELECT runtime_owner,runtime_kind FROM agents_meta WHERE id=%s", (agent_id,)
            )
        ).fetchone()
    return held_owner == (owner, "hosted")


@dataclass(frozen=True)
class _LegacyAdoption:
    """Evidence-backed proposal to replace a dead legacy owner before expiry."""

    owner: UUID
    generation: UUID | None
    lease_expires_at: datetime
    silence_s: float

    def matches(self, previous: tuple[Any, ...]) -> bool:
        """Whether the locked row still equals the exact observed target.

        Any concurrent write — a live owner renewing, a successor adopting —
        changes one of these fields and voids the evidence gathered for it.
        """
        return (previous[1], previous[0], previous[5]) == (
            self.owner,
            self.generation,
            self.lease_expires_at,
        )


async def _legacy_dead_host_adoption(
    pool: AsyncConnectionPool, agent_id: int, machine: str, owner: UUID
) -> _LegacyAdoption | None:
    """Propose admitting a legacy NULL row over a demonstrably dead local host.

    Legacy rows (``incarnation_resources IS NULL``) predate resource evidence
    and keep protocol zero, so the exact-process proof an evidence-carrying row
    offers does not exist for them. A same-machine successor may instead stand
    on this evidence set, re-pinned under the admission row lock by the caller
    (issue #2156; the 2026-09-10 force-restart stall where a fresh host waited
    out the dead predecessor's full lease):

    - renewal silence: the row's lease has not been renewed for
      ``LEGACY_HOST_ADOPTION_SILENCE_S`` — the predecessor's ownership beat
      stopped, not merely its owner UUID;
    - no live same-home agent-host daemon and no live exec child of this agent
      (``shared.host_process_evidence``) — no concurrent owner survives;
    - the row is unmarked (``last_turn_fatal_at IS NULL``): crash corpses keep
      their own reaper/resurrect recovery.

    NULL evidence alone never authorizes takeover: with any probe missing the
    proposal is None and the row keeps today's behavior — waiting for lease
    expiry. A refusal that a live process caused is logged with its facts.
    """
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT runtime_generation,runtime_owner,runtime_kind,machine,"
                "incarnation_resources,lease_expires_at,clock_timestamp(),"
                "last_turn_fatal_at IS NULL FROM agents_meta WHERE id=%s",
                (agent_id,),
            )
        ).fetchone()
    if row is None:
        return None
    generation, prior_owner, kind, row_machine, resources, lease, now, unmarked = row
    if (
        prior_owner is None
        or prior_owner == owner
        or row_machine != machine
        or kind != "hosted"
        or resources is not None
        or not unmarked
        or lease is None
    ):
        return None
    silence_s = AGENT_LEASE_TTL_S - (lease - now).total_seconds()
    if silence_s < LEGACY_HOST_ADOPTION_SILENCE_S:
        # The row still looks beaten by a live owner; it is not a candidate yet.
        return None
    evidence = await asyncio.to_thread(
        local_host_evidence, agent_id, ava_home(), exclude_pid=os.getpid()
    )
    if not evidence.clean:
        logger.info(
            "hosted legacy adoption blocked for agent {agent_id}: predecessor "
            "{predecessor} silent {silence}s but {reasons}",
            agent_id=agent_id,
            predecessor=str(prior_owner),
            silence=round(silence_s, 1),
            reasons="; ".join(evidence.blocking_reasons()),
        )
        return None
    return _LegacyAdoption(
        owner=prior_owner,
        generation=generation,
        lease_expires_at=lease,
        silence_s=silence_s,
    )


async def _dead_predecessor_evidence(
    previous: tuple[Any, ...],
    *,
    owner: UUID,
    machine: str,
    host: psutil.Process,
    legacy_adoption: _LegacyAdoption | None,
) -> tuple[ResourceProcess | None, bool]:
    """Which dead-predecessor proof licenses replacing this local row, if any.

    Runs inside the caller's metadata-lock transaction: the exact-process
    observation and the legacy proposal's pin are both bound to the locked row
    state. Returns the exited host process for the managed transfer and
    whether the legacy evidence replaces it. A live exact host raises the
    refusal that rolls the transaction back.
    """
    # Resolved at call time: the exact-exit probe is monkeypatched at its
    # source module in the resident tests.
    from shared.exec_owner_recovery import process_ended

    if previous[1] == owner or previous[3] != machine:
        return None, False
    if previous[4] is None:
        # A legacy NULL row can never prove its predecessor's exact exit —
        # there is no stored host process. The proposal is the replacement
        # proof, gathered before this transaction and pinned to the row state
        # it observed: it applies only while the locked row still matches.
        return None, legacy_adoption is not None and legacy_adoption.matches(previous)
    prior_resources = decode_resources(previous[4])
    if (
        isinstance(prior_resources, IncarnationResources)
        and not prior_resources.requests
        and prior_resources.frozen_by is None
        and prior_resources.host_process is not None
    ):
        # The row lock binds this monotonic exact-process observation to the
        # resource transfer in the same transaction.
        current_host = ResourceProcess(pid=host.pid, birth=stable_create_time(host))
        if prior_resources.host_process != current_host:
            if not await asyncio.to_thread(process_ended, prior_resources.host_process):
                _refuse_hosted_admission()
            return prior_resources.host_process, False
        # The same real host may transfer a closed incarnation only through
        # admit_resources_async's durable predecessor check.
    return None, False


async def admit_hosted_runtime(
    pool: AsyncConnectionPool,
    agent_id: int,
    machine: str,
    owner: UUID,
    *,
    expected_from: str,
    publication: RuntimeAdmission | None = None,
) -> RuntimeIncarnation | None:
    """Keep this owner's logical incarnation across turns; reject live others.

    A local legacy NULL row (no stored resource evidence) may be replaced
    before its lease expires only through the evidence-gated proposal of
    ``_legacy_dead_host_adoption``, re-checked under this row lock.
    """
    from shared.exec_owner_recovery import recover_local_resources

    if maintenance.held() and not await _held_owner_matches(pool, agent_id, owner):
        return None

    await asyncio.to_thread(recover_local_resources, agent_id, machine)
    native = psutil.Process()
    host_identity = ResourceProcess(pid=native.pid, birth=stable_create_time(native))
    if publication is None:
        publication = await asyncio.to_thread(process_runtime_admission)
    else:
        await asyncio.to_thread(publication.revalidate)
    legacy_adoption = await _legacy_dead_host_adoption(pool, agent_id, machine, owner)
    try:
        async with async_write_transaction(pool) as conn:
            publication_decision: AdmissionDecision | None
            try:
                publication_decision = await publication.decide_async(conn)
            except PublicationAdmissionDeferredError:
                # A pending publication / non-stable deployment phase freezes
                # ordinary *births*. A pending maintenance command is not one:
                # the held gates above already proved this boot owns the row
                # and has a command to consume, and the continuation runs no
                # graph work — it applies exactly that agent's restart after
                # the checkpoint flush. Deferring it here silently starved the
                # 2026-09-10 rollout's own drain: every held wake returned
                # without a receipt until the 300s timeout retained the hold
                # (issue #2159). The alternative fence below (a successor
                # boot) stays in force.
                if maintenance.pending_command(agent_id) is None:
                    return None
                publication_decision = None
            previous = await (
                await conn.execute(
                    "SELECT runtime_generation,runtime_owner,runtime_kind,machine,"
                    "incarnation_resources,lease_expires_at FROM agents_meta "
                    "WHERE id=%s FOR UPDATE",
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
            if publication_decision is not None:
                try:
                    require_current_for_managed(publication_decision, previous[4])
                except ResourceEvidenceError:
                    return None
            generation = (
                previous[0]
                if previous[1:3] == (owner, "hosted") and previous[0] is not None
                else uuid4()
            )
            exited_predecessor, legacy_adoption_used = await _dead_predecessor_evidence(
                previous,
                owner=owner,
                machine=machine,
                host=native,
                legacy_adoption=legacy_adoption,
            )

            await admit_resources_async(
                conn,
                RuntimeIncarnation(agent_id, generation, owner),
                host_identity,
                exited_predecessor=exited_predecessor,
            )
            # Exact local host death and resource closure are stronger than
            # its remaining lease; the same row lock protects both proofs. A
            # legacy NULL row has no exact process to prove: its re-pinned
            # evidence set stands in for the proof (issue #2156).
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
                    "OR lease_expires_at IS NULL OR lease_expires_at <= now() OR %s) "
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
                        exited_predecessor is not None or legacy_adoption_used,
                    ),
                )
            ).fetchone()
            if row is None:
                # Resource transfer and ordinary admission are one transaction.
                _refuse_hosted_admission()
            if legacy_adoption_used and legacy_adoption is not None:
                # The adoption audit: who was replaced, on what evidence, and
                # how stale the predecessor's ownership beat was. Recorded in
                # the same transaction that performed the takeover.
                await insert_event_log_async(
                    event_type="hosted_legacy_adoption",
                    agent_id=agent_id,
                    source="system",
                    payload={
                        "predecessor_owner": str(legacy_adoption.owner),
                        "lease_silence_s": round(legacy_adoption.silence_s, 1),
                        "same_home_host_daemons": 0,
                        "agent_exec_children": 0,
                    },
                )
                logger.info(
                    "hosted legacy adoption: admitted agent {agent_id} over dead local "
                    "predecessor {predecessor} after {silence}s of lease silence "
                    "(no live same-home host daemon, no live exec child)",
                    agent_id=agent_id,
                    predecessor=str(legacy_adoption.owner),
                    silence=round(legacy_adoption.silence_s, 1),
                )
            from agent.lifecycle_observe import observe_hosted_admission

            await observe_hosted_admission(conn, RuntimeIncarnation(agent_id, row[0], owner))
            await align_accepting_binding(
                conn, agent_id, RuntimeIncarnation(agent_id, row[0], owner)
            )
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
