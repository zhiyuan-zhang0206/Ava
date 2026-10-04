"""The owner notice every reclaim shares: a system inbound for a live agent."""

from __future__ import annotations

import psycopg

from base.agents.messages.inbound_provenance import InboundProvenance
from base.db import Database, insert_inbound_message, publish_inbound_wake
from base.events.live.bus import EventBus

# Batch ceiling per pass: the tables are small by design; the cap keeps a
# backlog (e.g. after a long outage) from turning one pass into a multi-minute
# transaction.
PASS_BATCH = 200

# The only agent states that can act on a reclamation notice. Terminated
# agents must NOT be resurrected by an expiry notification.
_NOTIFIABLE_STATUSES = ("running", "idling")


def notify_owner(
    conn: psycopg.Connection,
    db: Database,
    bus: EventBus,
    agent_id: int,
    content: str,
    *,
    source: str = "system",
) -> None:
    """Insert a system-sourced inbound for a live owner; never resurrects.

    Skipped for terminated agents — a reclamation
    notice is informational and must not wake a dead agent back up.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None or row[0] not in _NOTIFIABLE_STATUSES:
        return
    inbound_id = insert_inbound_message(
        conn,
        agent_id,
        content,
        source=source,
        provenance=InboundProvenance(source_verified_by=None, source_transport="ops"),
        database=db,
        bus=bus,
    )
    publish_inbound_wake(db, bus, agent_id, str(inbound_id))
