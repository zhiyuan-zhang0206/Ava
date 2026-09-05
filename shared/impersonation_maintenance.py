"""Lease expiration and bounded retention on the existing gateway TTL reaper."""

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
