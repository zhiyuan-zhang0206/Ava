"""Consume the API's sync requests.

`PUT /api/schedules/{id}` (edited script / enabled flag), start, stop, restart
and delete all need the schedule's session converged now, not at the next
reconcile. The gateway does not reach into this process: it upserts a row in
`schedule_sync_requests` (`gateway/schedules/session_control.py`) and waits a
bounded time for it to disappear. This module is the consumer.

A request is deleted only after its sync ran, and only if it is still the row
that was read (a newer request for the same schedule survives), so a crash
between the sync and the delete runs the sync again: it is idempotent, kill then
relaunch. While a maintenance hold is up nothing is consumed and the requests
stay queued.

Synchronous psycopg; the loop calls it through `asyncio.to_thread`.
"""

from __future__ import annotations

import logging

from psycopg_pool import ConnectionPool

from base.db.transaction import write_transaction
from services.wake.schedule_manager.manager import ScheduleManager

_log = logging.getLogger(__name__)

# One consume pass handles at most this many schedules; a larger burst drains
# over the next passes (the loop runs every second).
_BATCH = 50


def consume_requests(pool: ConnectionPool, manager: ScheduleManager) -> int:
    """Run the sync of every queued request, oldest first; return how many ran."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT schedule_id, requested_at FROM schedule_sync_requests "
            "ORDER BY requested_at, schedule_id LIMIT %s",
            (_BATCH,),
        )
        queued = cur.fetchall()
    handled = 0
    for schedule_id, requested_at in queued:
        if not manager.sync_one(schedule_id):
            break  # a maintenance hold: leave the rest queued
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM schedule_sync_requests WHERE schedule_id = %s AND requested_at = %s",
                (schedule_id, requested_at),
            )
        handled += 1
        _log.info("[schedule-manager] synced schedule %s on request", schedule_id)
    return handled
