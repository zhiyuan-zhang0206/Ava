"""Stalled crash-marked recovery request (task #3618).

A chat inbound stuck `pending` on a crash-marked idling corpse is the one
stall shape no other automatic path owns: the corpse never claims (its
process is gone), the corpse reaper waits out its grace window, and the
terminated-owner retry never sees the row (it is not terminated yet). The
watchdog escalating to the owner's home runner — one request per owner, a 60s
persisted cooldown, `_HARVEST_MAX_CONCURRENCY` in flight within a round —
gives every such delivery a bounded-time recovery decision (harvest now, or a
refusal naming its reason), which is the invariant behind the
`delivery_recovery_decision` event.

Runs as one resident sequential loop, so an owner never has two requests in
flight; gated by `delivery_stalled_recovery_enabled`, read each round.

Split out of `daemon.py` when its tick grew past the file ceiling (same split
as `dispatch_guard` / `resurrect_guard` / `turn_liveness`).
"""

from __future__ import annotations

import asyncio
import functools
import logging

from psycopg import sql
from psycopg_pool import ConnectionPool

from base import telemetry
from base.config import settings
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.events.live.bus import EventBus
from services.wake.delivery_watchdog import attempts, rounds

_log = logging.getLogger("services.wake.delivery_watchdog.stall_recovery")

# Same per-owner retry cadence as the G4 resurrect retry.
_HARVEST_RETRY_MIN_INTERVAL_S = 60.0
_HARVEST_MAX_CONCURRENCY = 2


def select_stalled_crash_marked(
    pool: ConnectionPool, threshold_s: float
) -> list[tuple[int, int, str | None, float]]:
    """One row per pending chat inbound past `threshold_s` whose owner is a
    crash-marked idling corpse, oldest first: `(inbound_id, agent_id, label,
    age_s)`. The owner predicate is the corpse reaper's own marker
    (`last_turn_fatal_at IS NOT NULL` on an idling row); an owner the
    recovery breaker halted (`RECOVERY_BREAKER_CLEAR`) or one with an
    in-force suppression window is excluded — automatic recovery must not
    start for it."""
    from base.agents.recovery_breaker import RECOVERY_BREAKER_CLEAR

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT m.id, m.agent_id, a.label, "
                "EXTRACT(EPOCH FROM (now() - m.created_at)) "
                "FROM inbound_messages m "
                "LEFT JOIN agents a ON a.id = m.agent_id "
                "JOIN agents_meta am ON am.id = m.agent_id "
                "WHERE m.status = 'pending' AND m.kind = 'chat' "
                "  AND am.status = 'idling' AND am.last_turn_fatal_at IS NOT NULL "
                "  AND (am.wake_suppressed_until IS NULL "
                "       OR am.wake_suppressed_until < now()) "
                "  AND {} "
                "  AND m.created_at < now() - make_interval(secs => %s) "
                "ORDER BY m.created_at ASC"
            ).format(sql.SQL(RECOVERY_BREAKER_CLEAR)),
            (threshold_s,),
        )
        return [(r[0], r[1], r[2], float(r[3])) for r in cur.fetchall()]


async def _request_harvest(
    pool: ConnectionPool, db: Database, bus: EventBus, agent_id: int, inbound_id: int
) -> None:
    """Ask the owner's home runner for one harvest decision under the RPC
    deadline; emit it (the recovery-decision-rate metric)."""
    from ops.lifecycle import recover_crash_marked_if_stalled

    try:
        try:
            async with asyncio.timeout(rounds.rpc_deadline_s()):
                decision, reason = await recover_crash_marked_if_stalled(
                    db, bus, agent_id, stalled_inbound_id=inbound_id
                )
        except Exception:
            _log.info(
                "[delivery] stalled crash-marked recovery request failed for agent %s",
                agent_id,
                exc_info=True,
            )
            decision, reason = "error", "harvest request failed"
    finally:
        await asyncio.to_thread(attempts.finish_attempt, pool, attempts.HARVEST, agent_id)
    detail = f" ({reason})" if reason else ""
    _log.info(
        "[delivery] stalled crash-marked recovery for agent %s (inbound %s): %s%s",
        agent_id,
        inbound_id,
        decision,
        detail,
    )
    try:
        telemetry.emit(
            "telemetry",
            "delivery_recovery_decision",
            agent_id=agent_id,
            source="system",
            attributes={"inbound_id": inbound_id, "decision": decision, "reason": reason},
        )
    except Exception:
        _log.exception(
            "[delivery] delivery_recovery_decision emit failed for inbound %s", inbound_id
        )


async def stall_recovery_round(
    pool: ConnectionPool, db: Database, bus: EventBus, progress: LoopProgress, threshold_s: float
) -> None:
    """For every stalled chat of a crash-marked idling corpse, request one
    harvest decision from its home runner, then return. Per-owner cooldown (the
    same cadence as the G4 resurrect retry, persisted); disabled by
    `delivery_stalled_recovery_enabled`."""
    if not settings.daemon.delivery_stalled_recovery_enabled:
        return
    rows = await asyncio.to_thread(select_stalled_crash_marked, pool, threshold_s)
    oldest_inbound: dict[int, int] = {}
    for inbound_id, agent_id, _label, _age_s in rows:
        oldest_inbound.setdefault(agent_id, inbound_id)
    claimed, _deferred = await asyncio.to_thread(
        attempts.claim_attempts,
        pool,
        attempts.HARVEST,
        list(oldest_inbound),
        _HARVEST_RETRY_MIN_INTERVAL_S,
    )
    await round_loop.fan_out(
        [functools.partial(_request_harvest, pool, db, bus, a, oldest_inbound[a]) for a in claimed],
        concurrency=_HARVEST_MAX_CONCURRENCY,
        progress=progress,
    )


async def stall_recovery_loop(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    progress: LoopProgress,
    interval_s: float,
    threshold_s: float,
) -> None:
    """The stalled crash-marked harvest as a resident sequential loop."""

    async def one_round() -> None:
        await stall_recovery_round(pool, db, bus, progress, threshold_s)

    await round_loop.run_rounds("stalled crash-marked recovery", progress, interval_s, one_round)
