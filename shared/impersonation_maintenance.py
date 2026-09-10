"""Lease expiration and bounded retention on the existing gateway TTL reaper."""

import shlex
import sys

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from shared._impersonation_store import expire, lock_lease
from shared.db import publish_inbound_wake
from shared.db_transaction import write_transaction


def reap_impersonations(pool: ConnectionPool, *, limit: int = 200) -> int:
    """Reconcile expired controllers even when their native runner is offline.

    Completed capability/journal records retire after seven days, only after
    checkpoint receipt and handoff consumption. The handoff message itself
    stays in the normal durable inbox/history.
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
    with write_transaction(pool) as conn:
        conn.execute(
            "WITH retired AS (SELECT p.id FROM agent_impersonations p "
            "LEFT JOIN inbound_messages i ON i.id=p.summary_inbound_id "
            "WHERE p.ended_at<clock_timestamp()-interval '7 days' "
            "AND p.status IN ('released','rejected','expired') AND p.delta_version=p.applied_version "
            "AND (p.summary_inbound_id IS NULL OR i.status='done') "
            "ORDER BY p.ended_at LIMIT %s) DELETE FROM agent_impersonations "
            "WHERE id IN (SELECT id FROM retired)",
            (limit,),
        )
    return len(expired_agents)


REMINDER_WINDOW_SECONDS = 300.0


def remind_expiring_impersonations(
    pool: ConnectionPool, *, window_seconds: float = REMINDER_WINDOW_SECONDS
) -> int:
    """Insert one pending renewal reminder per lease about to expire.

    Runs in the gateway TTL reaper cycle (default 60s), ahead of expiry
    reconciliation, so a lease gets its reminder within the 300s window with
    several scan chances. The reminder is an ordinary durable inbox row of
    kind='reminder' tagged with the lease id in its payload; the bound relay
    pushes it through the same envelope as any inbox message, and the external
    controller ACKs it the same way (with the same re-delivery window). One
    reminder per lease, ever: the NOT EXISTS below considers ANY reminder row
    for the lease — an ACKed ('done') row still counts, so a controller that
    ACKs without renewing or releasing is not nagged again every reaper cycle
    (issue #2054); an un-ACKed row is re-delivered by the relay until ACK or
    lease end. The reaper's expiry pass dismisses pending reminders whose
    lease ended, so a leftover can never reach the native agent's inbox.
    """
    reminded_agents: list[int] = []
    with write_transaction(pool) as conn:
        candidates = conn.execute(
            "SELECT l.id, l.agent_id, l.expires_at FROM agent_impersonations l "
            "WHERE l.status='active' "
            "AND l.expires_at<=clock_timestamp()+make_interval(secs=>%s) "
            "AND NOT EXISTS (SELECT 1 FROM inbound_messages r "
            "WHERE r.agent_id=l.agent_id AND r.kind='reminder' "
            "AND r.payload->>'lease_id'=l.id::text) "
            "ORDER BY l.expires_at LIMIT %s",
            (window_seconds, 200),
        ).fetchall()
        prefix = [sys.executable, "-m", "cli", "impersonate"]
        for lease_id, agent_id, expires_at in candidates:
            renew = shlex.join([*prefix, "renew", str(lease_id), "--ttl", "3600"])
            release = shlex.join([*prefix, "release", str(lease_id), "--summary", "..."])
            content = (
                f"Ava impersonation lease {lease_id} for agent {agent_id} expires at "
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
