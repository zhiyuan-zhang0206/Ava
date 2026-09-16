"""Lease expiration without deleting permanent history on the existing gateway TTL reaper."""

import shlex
import sys

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from shared._impersonation_store import expire, lock_lease
from shared.db import publish_inbound_wake
from shared.db_transaction import write_transaction

# One reaper pass handles at most this many leases per list — expired-lease
# reconciliation and the approaching-expiry reminder scan each take one page.
# The pass stays a short transaction and the next cycle (default 60s) picks up
# any remainder, so a backlog drains over cycles rather than one long pass.
_PASS_BATCH = 200


def reap_impersonations(pool: ConnectionPool, *, limit: int = _PASS_BATCH) -> int:
    """Reconcile expired controllers even when their native runner is offline.

    Session records, lifecycle events and messages are retained permanently.
    """
    with write_transaction(pool) as conn:
        candidates = conn.execute(
            "SELECT id FROM agent_impersonations WHERE status IN ('requested','accepted','active') "
            "AND expires_at<=clock_timestamp() ORDER BY agent_id LIMIT %s",
            (limit,),
        ).fetchall()
        expired_agents: list[int] = []
        for (lease_id,) in candidates:
            lease = lock_lease(conn, str(lease_id))
            if expire(conn, lease)["status"] == "expired":
                expired_agents.append(lease["agent_id"])
    for agent_id in expired_agents:
        publish_inbound_wake(agent_id, "impersonation-expired")
    return len(expired_agents)


# How long before expiry a lease first gets its renewal reminder: 300s (5
# minutes) is several 60s reaper cycles, so the reminder lands promptly and
# still leaves the controller time to renew before the lease lapses; one
# reminder per expiry deadline (issue #2054).
REMINDER_WINDOW_SECONDS = 300.0


def remind_expiring_impersonations(
    pool: ConnectionPool, *, window_seconds: float = REMINDER_WINDOW_SECONDS
) -> int:
    """Insert one pending renewal reminder per approaching expiry deadline.

    Runs in the gateway TTL reaper cycle (default 60s), ahead of expiry
    reconciliation, so a lease gets its reminder within the 300s window with
    several scan chances. The reminder is an ordinary durable inbox row of
    kind='reminder' tagged with the lease id in its payload; the bound relay
    pushes it through the same envelope as any inbox message, and the external
    controller ACKs it the same way (with the same re-delivery window). One
    reminder per expiry deadline: the NOT EXISTS below considers ANY reminder row
    for that deadline — an ACKed ('done') row still counts, so a controller that
    ACKs without renewing or releasing is not nagged again every reaper cycle
    (issue #2054); an un-ACKed row is re-delivered by the relay until ACK or
    lease end. The reaper's expiry pass dismisses pending reminders whose
    lease ended, so a leftover can never reach the native agent's inbox.
    """
    reminded_agents: list[int] = []
    with write_transaction(pool) as conn:
        candidates = conn.execute(
            "SELECT l.id, l.agent_id, l.expires_at, l.session_id FROM agent_impersonations l "
            "WHERE l.status='active' "
            "AND l.expires_at<=clock_timestamp()+make_interval(secs=>%s) "
            "AND NOT EXISTS (SELECT 1 FROM inbound_messages r "
            "WHERE r.agent_id=l.agent_id AND r.kind='reminder' "
            "AND r.payload->>'lease_id'=l.id::text "
            "AND (r.payload->>'expires_at')::timestamptz=l.expires_at) "
            "ORDER BY l.agent_id LIMIT %s",
            (window_seconds, _PASS_BATCH),
        ).fetchall()
        prefix = [sys.executable, "-m", "cli", "impersonate"]
        for lease_id, agent_id, expires_at, session_id in candidates:
            lease = lock_lease(conn, str(lease_id))
            if lease["status"] != "active" or lease["expires_at"] != expires_at:
                continue
            if (
                conn.execute(
                    "SELECT 1 FROM inbound_messages WHERE agent_id=%s AND kind='reminder' "
                    "AND payload->>'lease_id'=%s AND (payload->>'expires_at')::timestamptz=%s LIMIT 1",
                    (agent_id, str(lease_id), expires_at),
                ).fetchone()
                is not None
            ):
                continue
            renew = shlex.join(
                [*prefix, "renew", str(session_id), "--agent", str(agent_id), "--ttl", "3600"]
            )
            release = shlex.join(
                [*prefix, "release", str(session_id), "--agent", str(agent_id), "--summary", "..."]
            )
            content = (
                f"Ava impersonation session {session_id} for agent {agent_id} expires at "
                f"{expires_at:%Y-%m-%d %H:%M UTC}. Renew it to keep working, or "
                f"release it with a completion summary:\n{renew}\n{release}"
            )
            conn.execute(
                "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
                "VALUES(%s,%s,'reminder','system',%s)",
                (
                    agent_id,
                    content,
                    Jsonb({"lease_id": str(lease_id), "expires_at": expires_at.isoformat()}),
                ),
            )
            reminded_agents.append(agent_id)
        # Defensive sweep: dismiss reminders whose lease is no longer active.
        # Release/expiry transactions already dismiss their own; this catches
        # rows from a lease that ended before its reminder was ever read.
        conn.execute(
            "UPDATE inbound_messages r SET status='done' "
            "FROM agent_impersonations l "
            "WHERE r.kind='reminder' AND r.status='pending' "
            "AND r.payload->>'lease_id'=l.id::text "
            "AND l.status NOT IN ('requested','accepted','active')"
        )
    for agent_id in reminded_agents:
        publish_inbound_wake(agent_id, "impersonation-reminder")
    return len(reminded_agents)
