"""Lease expiration without deleting permanent history on the existing gateway TTL reaper."""

import shlex
from typing import Literal

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from shared._impersonation_store import expire, lock_lease
from shared.db import publish_inbound_wake
from shared.db_transaction import write_transaction
from shared.live_announce import publish_agent_updated_sync, publish_impersonation_changed_sync
from shared.machines import machine_home

# One reaper pass handles at most this many leases per list — expired-lease
# reconciliation and the approaching-expiry reminder scan each take one page.
# The pass stays a short transaction and the next cycle (default 60s) picks up
# any remainder, so a backlog drains over cycles rather than one long pass —
# an internal batch quantity, not a tuning knob (task #3696 exception inventory).
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
        publish_impersonation_changed_sync(agent_id)
        publish_agent_updated_sync(agent_id)
    return len(expired_agents)


def force_expire_impersonation(
    pool: ConnectionPool, agent_id: int, session_id: int, actor: str
) -> Literal["expired", "not_open"]:
    """Close only the open session the caller saw; return expired or not_open.

    The lease lock serializes this write with renewal, TTL expiry and the
    termination trigger. Terminating the agent remains the stronger fallback
    for a wedged native runtime; its trigger also revokes the lease.
    """
    from psycopg.rows import dict_row

    from shared._impersonation_store import dismiss_reminders, insert_handoff, lock_agent
    from shared.agents.impersonation_manifest import close_manifest_admission, is_protocol_v1
    from shared.impersonation_history import set_actor
    from shared.log import logger

    with write_transaction(pool) as conn:
        lock_agent(conn, agent_id)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM agent_impersonations WHERE agent_id=%s "
                "AND status IN ('requested','accepted','active') FOR UPDATE",
                (agent_id,),
            )
            lease = cur.fetchone()
        if lease is None or lease["session_id"] != session_id:
            return "not_open"
        set_actor(conn, actor)
        if is_protocol_v1(lease):
            close_manifest_admission(conn, str(lease["id"]))
        inbound_id = None
        if lease["status"] == "active" and not lease["automatic"]:
            inbound_id = insert_handoff(
                conn,
                lease,
                f"The external session {session_id} for this agent was ended by an operator. "
                "Control has returned to the agent. Unacknowledged messages remain pending; "
                "no external completion summary was supplied.",
                expired=True,
            )
        conn.execute(
            "UPDATE agent_impersonations SET status='expired',ended_at=clock_timestamp(), "
            "rejection_reason=%s,summary_inbound_id=%s WHERE id=%s",
            ("force-expired: ended by an operator", inbound_id, lease["id"]),
        )
        dismiss_reminders(conn, lease)
    logger.info(
        "impersonation force-expired",
        agent_id=agent_id,
        session_id=session_id,
        actor=actor,
    )
    publish_inbound_wake(agent_id, "impersonation-expired")
    publish_impersonation_changed_sync(agent_id)
    publish_agent_updated_sync(agent_id)
    return "expired"


# How long before expiry a lease first gets its renewal reminder: 300s (5
# minutes) is several 60s reaper cycles, so the reminder lands promptly and
# still leaves the controller time to renew before the lease lapses; one
# reminder per expiry deadline (issue #2054; task #3696 exception inventory).
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
    (issue #2054); an un-ACKed row uses the lease's configured relay budget before
    ACK or lease end. The reaper's expiry pass dismisses pending reminders whose
    lease ended, so a leftover can never reach the native agent's inbox.
    """
    reminded_agents: list[int] = []
    with write_transaction(pool) as conn:
        candidates = conn.execute(
            "SELECT l.id, l.agent_id, l.expires_at, l.session_id, l.machine "
            "FROM agent_impersonations l "
            "WHERE l.status='active' "
            "AND l.expires_at<=clock_timestamp()+make_interval(secs=>%s) "
            "AND NOT EXISTS (SELECT 1 FROM inbound_messages r "
            "WHERE r.agent_id=l.agent_id AND r.kind='reminder' "
            "AND r.payload->>'lease_id'=l.id::text "
            "AND (r.payload->>'expires_at')::timestamptz=l.expires_at) "
            "ORDER BY l.agent_id LIMIT %s",
            (window_seconds, _PASS_BATCH),
        ).fetchall()
        for lease_id, agent_id, expires_at, session_id, machine in candidates:
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
            home = machine_home(conn, machine)
            python = f"{home}/source/.venv/bin/python" if home else "~/.ava/source/.venv/bin/python"
            prefix = [python, "-m", "cli", "impersonate"]
            renew = shlex.join(
                [*prefix, "renew", str(session_id), "--agent", str(agent_id), "--ttl", "3600"]
            )
            release = shlex.join(
                [*prefix, "release", str(session_id), "--agent", str(agent_id), "--summary", "..."]
            )
            if home is None:
                # shlex quotes '~'; leave this fixed prefix unquoted for shell expansion.
                quoted_fallback = shlex.quote(python)
                renew = renew.replace(quoted_fallback, python, 1)
                release = release.replace(quoted_fallback, python, 1)
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
