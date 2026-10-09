"""Native compact-request insertion and its transactional audit."""

import psycopg

from base import telemetry
from base.agents.messages.inbound import InboundKind
from base.db import insert_inbound_message_in_transaction
from base.telemetry.audit_events import prepare_event_log, record_audit


def insert_compact_in_transaction(
    cur: psycopg.Cursor, agent: int
) -> tuple[int, telemetry.Event | None]:
    """Insert the compact envelope and audit without committing or emitting."""
    inbound, _ = insert_inbound_message_in_transaction(
        cur, agent, "", "user", kind=InboundKind.COMPACT_REQUEST.value
    )
    event = record_audit(
        cur.connection,
        prepare_event_log(
            event_type="compact",
            agent_id=agent,
            source="user",
            payload={"compact_kind": "request"},
        ),
    )
    return inbound, event
