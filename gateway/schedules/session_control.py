"""How the API reaches a schedule's session.

The sessions are kept alive by the `schedule-manager` service, a separate
process, so the API neither calls it nor holds its state. `request_sync` queues
a row in `schedule_sync_requests` (the service consumes it within about a
second, `services/wake/schedule_manager/requests.py`) and waits a bounded time for the
row to be consumed, so a start / stop / restart answers with the converged
status when the service is up and still answers when it is not. `capture` reads
the session's recent output straight from the shell backend: it needs no
manager state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from psycopg_pool import ConnectionPool

from base.cluster import session_name
from base.db.transaction import write_transaction
from base.sessions.backend import get_shell_backend

_log = logging.getLogger(__name__)

# How long a request waits to be consumed: the service's one-second poll plus
# the kill and relaunch it runs.
CONSUME_WAIT_S = 8.0
CONSUME_POLL_S = 0.2


def enqueue_blocking(pool: ConnectionPool[Any], schedule_id: int) -> None:
    """Queue (or refresh) the schedule's sync request. The row has no foreign
    key: a delete asks for the orphaned session to be killed after the schedule
    row is already gone."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedule_sync_requests (schedule_id) VALUES (%s) "
            "ON CONFLICT (schedule_id) DO UPDATE SET requested_at = clock_timestamp()",
            (schedule_id,),
        )


def _is_queued_blocking(pool: ConnectionPool[Any], schedule_id: int) -> bool:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM schedule_sync_requests WHERE schedule_id = %s", (schedule_id,))
        return cur.fetchone() is not None


async def wait_consumed(pool: ConnectionPool[Any], schedule_id: int) -> None:
    deadline = time.monotonic() + CONSUME_WAIT_S
    while time.monotonic() < deadline:
        if not await asyncio.to_thread(_is_queued_blocking, pool, schedule_id):
            return
        await asyncio.sleep(CONSUME_POLL_S)
    _log.warning(
        "schedule %s: sync request not consumed within %.0fs — the schedule-manager "
        "service will converge it when it is next up",
        schedule_id,
        CONSUME_WAIT_S,
    )


async def request_sync(pool: ConnectionPool[Any], schedule_id: int) -> None:
    """Ask the schedule-manager service to converge one schedule's session to its
    DB `enabled` state now (kill it, then relaunch if enabled, clearing its crash
    backoff), and wait a bounded time for it to do so."""
    await asyncio.to_thread(enqueue_blocking, pool, schedule_id)
    await wait_consumed(pool, schedule_id)


async def capture(schedule_id: int, lines: int) -> str | None:
    """The schedule session's recent output, or None when no session is live."""
    return await asyncio.to_thread(capture_blocking, schedule_id, lines)


def capture_blocking(schedule_id: int, lines: int) -> str | None:
    name = session_name(f"schedule-{schedule_id}")
    backend = get_shell_backend()
    if not backend.has_session(name):
        return None
    return backend.capture_pane(name, lines)
