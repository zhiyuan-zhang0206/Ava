"""Terminated-owner resurrect retry loop (G4).

A pending chat whose owner is terminated means the delivery-path auto-resurrect
failed (or the delivery predates it). This loop retries it, bounded against
storms and against unbounded age: past the stale-claimed threshold the chat is
a dead letter and its owner is never resurrected for it again (issue #2049).

  * one round at a time — an owner can never be in two attempts at once;
  * per-agent cooldown (`delivery_watchdog_attempts`, survives restarts) — a
    failed resurrect (unreachable home machine) is re-attempted at most once a
    minute, counted from the attempt's end;
  * concurrency semaphore — at most 2 resurrects in flight within a round;
  * per-round cap (`delivery_watchdog_max_resurrect_per_tick`) — a pile of
    dead letters drains over rounds, never as a burst.

Consecutive failures escalate into a durable wake-suppression window
(`resurrect_guard`).
"""

from __future__ import annotations

import asyncio
import functools
import logging

from psycopg import sql
from psycopg_pool import ConnectionPool

from base.agents import AgentStatus
from base.agents.messages.inbound import InboundKind
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.events.live.bus import EventBus
from services.wake.delivery_watchdog import attempts, resurrect_guard, rounds

_log = logging.getLogger("services.wake.delivery_watchdog.resurrect_retry")

_RESURRECT_RETRY_MIN_INTERVAL_S = 60.0
_RESURRECT_MAX_CONCURRENCY = 2


def select_terminated_owners_with_pending(
    pool: ConnectionPool,
    threshold_s: float,
) -> list[tuple[int, int]]:
    """One `(agent_id, trigger_inbound_id)` per terminated owner with a
    post-termination pending chat, ordered by agent id.

    The selected chat is carried to the home runner as the final resurrection
    CAS. A chat already pending when the agent was terminated cannot reverse
    that explicit lifecycle decision — EXCEPT when the system itself reaped a
    crash-marked corpse (`SYSTEM_REAPED_CRASH_ROW`): that death was not an
    operator's will, so leftover work still resumes its owner. A later
    termination makes this trigger stale before it can launch, and a tripped
    recovery breaker (`RECOVERY_BREAKER_CLEAR`) or an active wake suppression
    keeps automatic recovery halted entirely. Chat only: lifecycle kinds
    (terminate / restart) must not resurrect a dead agent against the caller's
    intent. A pile of 250 dead letters for one agent still means one attempt,
    not 250.

    System notices never resurrect: a system-family chat (`system` /
    `system:<subtype>`) is a framework notification, not a person or peer
    message — it waits for the owner's next resurrect, or the stale threshold
    closes it. Machine *wakeups* still wake; exempt are the recovery-class
    chats (`hosted_turn_recovery` marker): the watchdog's wedged-turn wake and
    the corpse reaper's crash-recovery wake (task #4039) revive their owner.

    `threshold_s` bounds how long a pending chat keeps its terminated owner a
    resurrect candidate: past it the row is a dead letter (issue #2049) that
    `dead_letter_stale_pending_chats` closes — and with it the trigger, so no
    unbounded retry can resurrect-suicide the agent forever.
    """
    from base.agents.incarnation.lifecycle_acceptance import (
        FAILED_RESTART_FOR_CURRENT_TARGET,
        SYSTEM_NOTICE_SOURCE,
        SYSTEM_REAPED_CRASH_ROW,
    )
    from base.agents.recovery_breaker import RECOVERY_BREAKER_CLEAR

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT m.agent_id, MIN(m.id) "
                "FROM inbound_messages m "
                "JOIN agents_meta ON agents_meta.id = m.agent_id "
                "WHERE m.status = 'pending' AND m.kind = 'chat' "
                "  AND agents_meta.status = 'terminated' AND NOT {} "
                " AND (m.created_at > agents_meta.status_changed_at OR {}) "
                "  AND m.created_at > now() - make_interval(secs => %s) "
                "  AND m.id > COALESCE(agents_meta.last_force_terminate_inbound_id, 0) "
                "  AND (agents_meta.wake_suppressed_until IS NULL "
                "       OR agents_meta.wake_suppressed_until < now()) "
                "  AND {} "
                "  AND NOT {} "
                "GROUP BY m.agent_id "
                "ORDER BY m.agent_id"
            ).format(
                sql.SQL(FAILED_RESTART_FOR_CURRENT_TARGET),
                sql.SQL(SYSTEM_REAPED_CRASH_ROW),
                sql.SQL(RECOVERY_BREAKER_CLEAR),
                sql.SQL(SYSTEM_NOTICE_SOURCE),
            ),
            (threshold_s,),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


async def resurrect_one(
    pool: ConnectionPool, db: Database, bus: EventBus, agent_id: int, trigger_inbound_id: int
) -> None:
    """Run `resurrect_if_terminated` for one claimed agent under the RPC
    deadline; classify the returned status and escalate consecutive failures
    into a durable wake-suppression window."""
    from ops.lifecycle import resurrect_if_terminated

    try:
        try:
            async with asyncio.timeout(rounds.rpc_deadline_s()):
                status = await resurrect_if_terminated(
                    db,
                    bus,
                    agent_id,
                    trigger_inbound_id=trigger_inbound_id,
                    trigger_inbound_kind=InboundKind.CHAT,
                )
        except Exception:
            _log.warning(
                "[delivery] resurrect retry failed for agent %s",
                agent_id,
                exc_info=True,
            )
            failed = True
        else:
            failed = status is AgentStatus.TERMINATED
            if not failed:
                _log.info(
                    "[delivery] resurrect retry for terminated agent %s -> status %s",
                    agent_id,
                    status,
                )
        if failed:
            await asyncio.to_thread(resurrect_guard.record_resurrect_failure, pool, agent_id)
        else:
            await asyncio.to_thread(resurrect_guard.record_resurrect_success, pool, agent_id)
    finally:
        await asyncio.to_thread(attempts.finish_attempt, pool, attempts.RESURRECT, agent_id)


async def resurrect_round(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    progress: LoopProgress,
    max_per_round: int,
    threshold_s: float,
) -> None:
    """Retry one resurrect per distinct terminated owner with a pending chat
    whose cooldown has elapsed, at most `max_per_round` of them, and return
    when they are done."""
    owners = await asyncio.to_thread(select_terminated_owners_with_pending, pool, threshold_s)
    trigger_of = dict(owners)
    claimed, deferred = await asyncio.to_thread(
        attempts.claim_attempts,
        pool,
        attempts.RESURRECT,
        list(trigger_of),
        _RESURRECT_RETRY_MIN_INTERVAL_S,
        max_per_round,
    )
    if deferred:
        _log.warning(
            "[delivery] resurrect retry backlog: %s more terminated owner(s) deferred",
            deferred,
        )
    if claimed:
        _log.info("[delivery] retrying %s resurrect(s)", len(claimed))
    await round_loop.fan_out(
        [functools.partial(resurrect_one, pool, db, bus, a, trigger_of[a]) for a in claimed],
        concurrency=_RESURRECT_MAX_CONCURRENCY,
        progress=progress,
    )


async def resurrect_loop(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    progress: LoopProgress,
    interval_s: float,
    max_per_round: int,
    threshold_s: float,
) -> None:
    """The resurrect retry as a resident sequential loop."""

    async def one_round() -> None:
        await resurrect_round(pool, db, bus, progress, max_per_round, threshold_s)

    await round_loop.run_rounds("resurrect retry", progress, interval_s, one_round)
