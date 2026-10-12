"""Mark crashed hosted agents terminated and publish failure diagnostics."""

from __future__ import annotations

from typing import NamedTuple
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from base import telemetry
from base.db.transaction import async_write_transaction
from base.deploy.progress_timeout import CORPSE_REAP_GRACE_S
from base.events.live.announce import publish_agent_updated
from base.events.live.bus import EventBus
from base.log import logger
from base.telemetry.audit_events import prepare_event_log, record_audit_async


class ReapedCorpse(NamedTuple):
    """One committed termination; no recovery input is created."""

    agent_id: int


async def reap_crash_corpses(
    pool: AsyncConnectionPool,
    machine: str,
    owner: UUID,
    *,
    bus: EventBus,
) -> list[ReapedCorpse]:
    """Terminate crash-marked idling corpses whose grace window has elapsed.

    The positive death signal is the row's own `last_turn_fatal_at` (stamped
    firsthand by this host when the turn died), never staleness alone — an
    unmarked idling row is a live agent and is never touched. Idling-only:
    a `running` row has a live invocation (or claim park) in flight. The
    incarnation CAS already happened at stamp time, so the mark names exactly
    the generation that died; a row woken in the grace window either completes
    a turn (clears the mark) or parks and keeps it.

    Returns the reaped agents; mounted frontends are refreshed best-effort
    after the durable flip.

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
                "termination_source = 'reaper', lease_expires_at = NULL "
                "WHERE machine = %s AND runtime_kind = 'hosted' AND status = 'idling' "
                "AND last_turn_fatal_at IS NOT NULL "
                "AND last_turn_fatal_at <= now() - make_interval(secs => %s) "
                "AND (runtime_owner = %s OR lease_expires_at IS NULL "
                "OR lease_expires_at <= now()) "
                "RETURNING id",
                (machine, CORPSE_REAP_GRACE_S, owner),
            )
        ).fetchall()
        reaped: list[ReapedCorpse] = []
        recorded: list[telemetry.Event] = []
        for (agent_id,) in rows:
            event = prepare_event_log(
                event_type="status_change",
                agent_id=agent_id,
                source="system",
                payload={"from": "idling", "to": "terminated", "reason": "corpse_reaper"},
            )
            recorded.append(await record_audit_async(conn, event))
            reaped.append(
                ReapedCorpse(
                    agent_id,
                )
            )
    for event in recorded:
        telemetry.emit_prepared(event)
    if reaped:
        logger.info(
            "corpse reaper: terminated {n} crash-dead row(s)",
            event="corpse_reaper_terminated",
            n=len(reaped),
        )
    await _publish_reaped_corpses(bus, [corpse.agent_id for corpse in reaped])
    return reaped


async def _publish_reaped_corpses(bus: EventBus, reaped: list[int]) -> None:
    """Best-effort refresh of mounted frontends; the durable flip already committed."""
    for agent_id in reaped:
        try:
            await publish_agent_updated(bus, agent_id)
        except Exception:
            logger.exception(
                "corpse reap lifecycle hint publish failed",
                event="corpse_reaper_publish_failed",
                agent_id=agent_id,
            )
