"""State signal for impersonation event logs that cannot complete on their own.

Both conditions are facts about rows, so there is no threshold: an ended lease
still waiting on an open source (``seal_stuck``, clears when the source seals)
and a lease with a source whose capture failed (``capture_failed``, permanent:
a failed source never completes its lease). Every pass emits one
``impersonation_event_log_incomplete`` event per lease and condition while the
fact holds; an alert rule over the event stream owns the notification.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from base.agents.impersonation.event_log import LOG_PROTOCOL_VERSION
from base.log import logger

_INCOMPLETE_LEASES = """
WITH leases AS (
    SELECT l.id, l.agent_id, l.session_id, l.machine, l.event_delivery_pending_reason,
        (l.ended_at IS NOT NULL AND EXISTS (
            SELECT 1 FROM agent_impersonation_event_participants p
            WHERE p.lease_id = l.id AND p.state = 'open')) AS seal_stuck,
        EXISTS (
            SELECT 1 FROM agent_impersonation_event_participants p
            WHERE p.lease_id = l.id AND p.state = 'failed') AS capture_failed
    FROM agent_impersonations l
    WHERE l.automatic AND l.event_delivery_protocol_version = %s
        AND l.events_completed_at IS NULL
)
SELECT * FROM leases WHERE seal_stuck OR capture_failed
"""


def emit_incomplete_event_logs(conn: psycopg.Connection[Any]) -> int:
    """Emit the incomplete-event-log signal for every lease it holds for.

    Returns the number of events emitted.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_INCOMPLETE_LEASES, (LOG_PROTOCOL_VERSION,))
        leases = cur.fetchall()
    emitted = 0
    for lease in leases:
        for condition in ("seal_stuck", "capture_failed"):
            if not lease[condition]:
                continue
            logger.warning(
                "impersonation event log cannot complete: {condition} (lease {lease_id})",
                event="impersonation_event_log_incomplete",
                agent_id=lease["agent_id"],
                condition=condition,
                lease_id=str(lease["id"]),
                lease_machine=str(lease["machine"]),
                pending_reason=str(lease["event_delivery_pending_reason"] or "unknown"),
                session=f"{lease['agent_id']}:{lease['session_id']}",
            )
            emitted += 1
    return emitted
