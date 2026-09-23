"""Runner-owned replay of late SDK/API facts into permanent session handoffs."""

import asyncio

from psycopg.rows import dict_row

from ava._impersonation_events import consume_recorded_events
from shared import maintenance
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.log import logger
from shared.machine import machine_name


def reconcile_one() -> None:
    """Choose one due local session fairly, independent of agent/model liveness."""
    with write_transaction() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM agent_impersonations WHERE machine=%s AND automatic "
            "AND activated_at IS NOT NULL AND ended_at IS NOT NULL "
            "AND events_completed_at IS NULL AND events_next_read_at<=clock_timestamp() "
            "ORDER BY events_next_read_at,agent_id,session_id LIMIT 1",
            (machine_name(),),
        )
        session = cur.fetchone()
        if session is None:
            return
        # Advancing the due time schedules a retry, not a completeness receipt.
        # Even a failing session rotates behind other pending sessions.
        cur.execute(
            "UPDATE agent_impersonations SET events_next_read_at=clock_timestamp()+%s*interval '1 second' "
            "WHERE id=%s",
            (settings.general.impersonation_event_reconcile_interval_seconds, session["id"]),
        )
    consume_recorded_events(session, page_budget=settings.general.impersonation_event_page_budget)


async def reconcile_forever() -> None:
    """Replay at the runner's maintenance cadence without blocking ownership renewal."""
    while True:
        if not maintenance.quiesced():
            try:
                await asyncio.to_thread(reconcile_one)
            except Exception:
                logger.exception("Impersonation event replay failed; pending session will retry")
        await asyncio.sleep(settings.general.impersonation_event_reconcile_interval_seconds)
