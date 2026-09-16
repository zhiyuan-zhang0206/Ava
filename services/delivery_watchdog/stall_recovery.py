"""Stalled crash-marked recovery request (task #3618).

A chat inbound stuck `pending` on a crash-marked idling corpse is the one
stall shape no other automatic path owns: the corpse never claims (its
process is gone), the corpse reaper waits out its grace window, and the
terminated-owner retry never sees the row (it is not terminated yet). The
watchdog escalating to the owner's home runner — one request per owner, a 60s
cooldown, `_HARVEST_MAX_CONCURRENCY` in flight — gives every such delivery a
bounded-time recovery decision (harvest now, or a refusal naming its reason),
which is the invariant behind the `delivery_recovery_decision` event.

Split out of `daemon.py` when its tick grew past the file ceiling (same split
as `dispatch_guard` / `resurrect_guard` / `turn_liveness`).
"""

from __future__ import annotations

import asyncio
import logging
import time

from psycopg import sql
from psycopg_pool import ConnectionPool

from shared import telemetry
from shared.config import settings

_log = logging.getLogger("services.delivery_watchdog.stall_recovery")

# Same per-owner retry cadence as the G4 resurrect retry in `daemon.py`; kept
# local because `daemon` imports this module (sharing the constant would be an
# import cycle).
_HARVEST_RETRY_MIN_INTERVAL_S = 60.0
_HARVEST_MAX_CONCURRENCY = 2
_harvest_tasks: dict[int, asyncio.Task[None]] = {}
_last_harvest_attempt: dict[int, float] = {}
_harvest_semaphore = asyncio.Semaphore(_HARVEST_MAX_CONCURRENCY)


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
    from shared.recovery_breaker import RECOVERY_BREAKER_CLEAR

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


async def _request_harvest(agent_id: int, inbound_id: int) -> None:
    """Ask the owner's home runner for one harvest decision; emit it (the
    recovery-decision-rate metric). Never raises."""
    from ops.ops_lifecycle import recover_crash_marked_if_stalled

    async with _harvest_semaphore:
        try:
            decision, reason = await recover_crash_marked_if_stalled(
                agent_id, stalled_inbound_id=inbound_id
            )
        except Exception:
            _log.info(
                "[delivery] stalled crash-marked recovery request failed for agent %s",
                agent_id,
                exc_info=True,
            )
            decision, reason = "error", "harvest request failed"
        finally:
            _last_harvest_attempt[agent_id] = time.monotonic()
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


def maybe_request_stall_recovery(pool: ConnectionPool, threshold_s: float) -> None:
    """For every stalled chat of a crash-marked idling corpse, request one
    harvest decision from its home runner. Per-owner single flight + 60s
    cooldown (the same retry cadence as the G4 resurrect retry); disabled by
    `delivery_stalled_recovery_enabled`. Fire-and-forget, like the resurrect
    retry — the tick never blocks on an RPC timeout."""
    if not settings.daemon.delivery_stalled_recovery_enabled:
        return
    now = time.monotonic()
    for inbound_id, agent_id, _label, _age_s in select_stalled_crash_marked(pool, threshold_s):
        if agent_id in _harvest_tasks:
            continue
        if now - _last_harvest_attempt.get(agent_id, 0.0) < _HARVEST_RETRY_MIN_INTERVAL_S:
            continue
        task = asyncio.create_task(_request_harvest(agent_id, inbound_id))
        _harvest_tasks[agent_id] = task

        def _discard_completed_task(
            completed: asyncio.Task[None], *, completed_agent_id: int = agent_id
        ) -> None:
            if _harvest_tasks.get(completed_agent_id) is completed:
                del _harvest_tasks[completed_agent_id]

        task.add_done_callback(_discard_completed_task)
