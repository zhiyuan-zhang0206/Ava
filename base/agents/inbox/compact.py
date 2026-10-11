"""The native compact request inbound, written for graph and history producers."""

import psycopg

from base.agents.inbox.insert import emit_prepared_event
from base.agents.inbox.wake import publish_inbound_wake
from base.agents.messages.control_delivery import insert_compact_in_transaction
from base.db import Database
from base.events.live.bus import EventBus


def insert_compact_request_inbound(
    db: psycopg.Connection, agent_id: int, *, database: Database, bus: EventBus
) -> int:
    """Insert the native compact envelope used by graph/history producers.

    Claim generates a summary and replaces messages. Public manual compaction
    uses observed closed-history admission instead of this primitive.
    Agent-authored summaries use ``ava.self.compact`` and ``compact_summary``.
    """
    with db.cursor() as cur:
        inbound_id, compact_event = insert_compact_in_transaction(cur, agent_id)
    db.commit()
    emit_prepared_event(compact_event)
    # Publish to Redis for agent wake-up (see insert_inbound_message + the
    # publish_inbound_wake docstring).
    publish_inbound_wake(database, bus, agent_id, str(inbound_id))
    return inbound_id
