"""Agent-to-agent message edges: who sent to whom, when it was sent, when the receiver took it.

Both ends are the agent level and nothing finer. The sent time is the audit event's time
(written in the transaction that inserts the inbound row); the read time is the inbound row's
`claimed_at`, the moment the receiver's claim step took it. The model sees the message at its
next request, so this is the earliest the receiver can have read it. An exact per-message read
time exists only in the receiver's checkpoint, which this view does not load.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from services.derived.insights.cluster.curves import INBOUND_ID_SQL
from services.derived.insights.cluster.schemas import ClusterMessages, ClusterWindow, MessageEdge

PREVIEW_CHARS = 160

_WHERE = (
    "a.event_name = 'send_message' AND a.agent_id = ANY(%s) AND a.target_agent_id = ANY(%s) "
    "AND a.ts >= %s AND a.ts < %s"
)
_COUNT_SQL = f"SELECT count(*) FROM audit_events a WHERE {_WHERE}"  # noqa: S608
_EDGE_SQL = f"""
    SELECT {INBOUND_ID_SQL}, a.target_agent_id, a.agent_id, a.ts, im.claimed_at,
           left(COALESCE(a.attributes->>'content', ''), %s)
    FROM audit_events a
    LEFT JOIN inbound_messages im ON im.id = {INBOUND_ID_SQL}
    WHERE {_WHERE}
    ORDER BY a.ts, a.id
    LIMIT %s
"""  # noqa: S608 — constant fragments only


def read(
    conn: psycopg.Connection[Any],
    agent_ids: list[int],
    start: datetime,
    end: datetime,
    limit: int,
) -> ClusterMessages:
    """The earliest `limit` messages among `agent_ids` in `[start, end)`, and how many there are."""
    window_args = (agent_ids, agent_ids, start, end)
    row = conn.execute(_COUNT_SQL, window_args).fetchone()
    total = int(row[0]) if row is not None else 0
    edges = [
        MessageEdge(
            inbound_id=None if inbound_id is None else int(inbound_id),
            sender=int(sender),
            receiver=int(receiver),
            sent_at=sent_at,
            read_at=claimed_at,
            preview=preview,
        )
        for inbound_id, sender, receiver, sent_at, claimed_at, preview in conn.execute(
            _EDGE_SQL, (PREVIEW_CHARS, *window_args, limit)
        )
    ]
    return ClusterMessages(
        window=ClusterWindow(from_=start, to=end),
        total=total,
        truncated=total > len(edges),
        edges=edges,
    )
