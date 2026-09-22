"""Durable relay attempt reservations; they never acknowledge processing.

Reserve before calling the host. Submission and SQL cannot commit atomically:
if the host accepted a push before the relay crashed, recording afterwards
would allow unlimited duplicates across restarts. An ambiguous or failed send
therefore consumes an attempt; its body stays pending for native handoff.
"""

from shared._impersonation_store import (
    ACK_WINDOW_SECONDS,
    MAX_DELIVERY_ATTEMPTS,
    authenticate_relay,
    expire,
    lock_lease,
    require_relay_active_locked,
)
from shared.db_transaction import write_transaction


def reserve_delivery(lease_id: str, relay_token: str, message_ids: list[int]) -> frozenset[int]:
    """Claim due, previously read pending rows before one host submission.

    The agent/lease lock serializes with ACK, release, and other relay sends.
    Recheck expiry here, committing any timeout even when nothing can be sent;
    the relay observes the terminal lease on its next read. Stale snapshots
    cannot spend an early retry or push a third time. A rotated credential
    cannot reserve; the new relay retains the previous relay's attempt count.
    """
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        authenticate_relay(lease, relay_token)
        lease = expire(conn, lease)
        if lease["status"] != "active":
            return frozenset()
        require_relay_active_locked(conn, lease, relay_token)
        rows = conn.execute(
            "UPDATE agent_impersonation_messages m "
            "SET delivery_attempts=m.delivery_attempts+1,last_delivery_at=clock_timestamp() "
            "FROM inbound_messages i WHERE m.lease_id=%s AND m.inbound_id=ANY(%s) "
            "AND i.id=m.inbound_id AND i.agent_id=%s AND i.status='pending' "
            "AND m.acknowledged_at IS NULL AND m.delivery_attempts < %s "
            "AND (m.last_delivery_at IS NULL OR m.last_delivery_at <= "
            "clock_timestamp() - %s*interval '1 second') RETURNING m.inbound_id",
            (lease_id, message_ids, lease["agent_id"], MAX_DELIVERY_ATTEMPTS, ACK_WINDOW_SECONDS),
        ).fetchall()
    return frozenset(row[0] for row in rows)
