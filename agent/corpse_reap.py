"""The hosted corpse reaper: terminate crash-dead rows the mark names.

One death buys one grace window (`CORPSE_REAP_GRACE_S`) — the beat reaper's
pass terminates marked idling corpses once it elapses (task #2609). The
prompt reap is the same termination moved up to the first re-crash: a mark
whose retry died again has spent its grace, and waiting out the rest only
lets a zombie claim and re-die (task #3616, the #3602 window). Both paths
share the termination shape and its events; only the trigger differs.

Split out of `agent.hosted_ownership` when the recrash reap pushed that
module at the 800-line budget ceiling.
"""

from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from shared.audit_events import insert_event_log_async
from shared.db_transaction import async_write_transaction
from shared.deploy_timing import CORPSE_REAP_GRACE_S
from shared.live_announce import publish_agent_updated
from shared.log import logger
from shared.runtime_incarnation import RuntimeIncarnation


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

    Returns the reaped agent ids; their mounted frontends are refreshed
    best-effort after the durable flip.

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
    await _publish_reaped_corpses(pool, reaped)
    return reaped


async def _publish_reaped_corpses(pool: AsyncConnectionPool, reaped: list[int]) -> None:
    """Best-effort refresh of mounted frontends; the durable flip already committed."""
    for agent_id in reaped:
        try:
            await publish_agent_updated(pool, agent_id)
        except Exception:
            logger.exception(
                "corpse reap snapshot publish failed",
                event="corpse_reaper_publish_failed",
                agent_id=agent_id,
            )


# The confirmed crash count a prompt reap attests: the mark's first stamp and
# the crash being settled. The mechanism fires at the first confirmed
# re-crash, when the retry under the mark has already failed.
RECRASH_CONFIRMED_CRASHES = 2


async def reap_recrashed_corpse(
    pool: AsyncConnectionPool,
    incarnation: RuntimeIncarnation,
) -> list[int]:
    """Terminate the corpse this incarnation just re-crashed (task #3616).

    The mark's first stamp already bought the corpse its grace window — the
    one chance to self-heal. A turn that dies AGAIN under the same mark is the
    retry failing, and the rest of the grace can only be spent by a zombie
    that keeps claiming and re-dying (the #3602 window). The corpse reaper's
    termination therefore runs now, from the settle point that witnessed the
    crash: same semantics and events as `reap_crash_corpses` — idling-only,
    `termination_source='reaper'`, the `corpse_reaper` status change and the
    `corpse_reaper_terminated` beat entry — with the confirmed crash count on
    both.

    The owner/generation pin is this path's lease qualification: only the
    exact incarnation that crashed can reap its row, so a row that moved on
    (a concurrent admission or replacement took it) matches nothing and is
    left alone — fail-closed against the concurrent transition.
    """
    async with async_write_transaction(pool) as conn:
        rows = await (
            await conn.execute(
                "UPDATE agents_meta SET status = 'terminated', "
                "termination_source = 'reaper', lease_expires_at = NULL, "
                "runtime_protocol_version = 0 "
                "WHERE id = %s AND status = 'idling' AND runtime_kind = 'hosted' "
                "AND last_turn_fatal_at IS NOT NULL "
                "AND runtime_generation = %s AND runtime_owner = %s "
                "RETURNING id",
                (incarnation.agent_id, incarnation.generation, incarnation.owner),
            )
        ).fetchall()
        for (agent_id,) in rows:
            await insert_event_log_async(
                event_type="status_change",
                agent_id=agent_id,
                source="system",
                payload={
                    "from": "idling",
                    "to": "terminated",
                    "reason": "corpse_reaper",
                    "crash_count": RECRASH_CONFIRMED_CRASHES,
                },
            )
    reaped = [row[0] for row in rows]
    if reaped:
        logger.info(
            "corpse reaper: prompt-reaped {n} re-crashed corpse(s) — the mark's grace was spent",
            event="corpse_reaper_terminated",
            n=len(reaped),
            crash_count=RECRASH_CONFIRMED_CRASHES,
        )
    await _publish_reaped_corpses(pool, reaped)
    return reaped
