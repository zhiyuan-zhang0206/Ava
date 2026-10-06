"""Lease expiration without deleting permanent history on the existing TTL reaper."""

import shlex
from typing import Literal

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.impersonation._store import expire, lock_lease
from base.agents.impersonation.status import ImpersonationStatus, parse_lease
from base.db import Database, publish_inbound_wake
from base.db.transaction import write_transaction
from base.events.live.announce import (
    publish_agent_updated_sync,
    publish_impersonation_changed_sync,
)
from base.events.live.bus import EventBus

# One reaper pass handles at most this many leases per list — expired-lease
# reconciliation and the approaching-expiry reminder scan each take one page.
# The pass stays a short transaction and the next cycle (default 60s) picks up
# any remainder, so a backlog drains over cycles rather than one long pass —
# an internal batch quantity, not a tuning knob (task #3696 exception inventory).
_PASS_BATCH = 200


def reap_impersonations(
    pool: ConnectionPool, db: Database, bus: EventBus, *, limit: int = _PASS_BATCH
) -> int:
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
            if expire(conn, lease)["status"] == ImpersonationStatus.EXPIRED:
                expired_agents.append(lease["agent_id"])
    for agent_id in expired_agents:
        publish_inbound_wake(db, bus, agent_id, "impersonation-expired")
        publish_impersonation_changed_sync(bus, agent_id)
        publish_agent_updated_sync(bus, agent_id)
    return len(expired_agents)


def signal_incomplete_event_logs(pool: ConnectionPool) -> int:
    """Emit the incomplete-event-log signal for every lease still holding it.

    A state signal, not a threshold: a lease cannot complete until its source
    seals (or ever, after a capture failure), and nothing else will say so.
    Re-emitted every pass while the fact holds.
    """
    from base.agents.impersonation_event_signals import emit_incomplete_event_logs
    from base.log import logger

    try:
        with pool.connection() as conn:
            return emit_incomplete_event_logs(conn)
    except Exception:
        # A failing signal pass must not stop the reclamation that runs after it.
        logger.exception("impersonation event-log signal pass failed")
        return 0


def force_expire_impersonation(
    pool: ConnectionPool, db: Database, bus: EventBus, agent_id: int, session_id: int, actor: str
) -> Literal["expired", "not_open"]:
    """Close only the open session the caller saw; return expired or not_open.

    The lease lock serializes this write with renewal, TTL expiry and the
    termination trigger. Terminating the agent remains the stronger fallback
    for a wedged native runtime; its trigger also revokes the lease.
    """
    from psycopg.rows import dict_row

    from base.agents.impersonation._store import (
        dismiss_reminders,
        insert_handoff,
        lock_agent,
    )
    from base.agents.impersonation.event_log import is_log_native
    from base.agents.impersonation.history import set_actor
    from base.agents.impersonation_manifest import close_event_admission
    from base.log import logger

    with write_transaction(pool) as conn:
        lock_agent(conn, agent_id)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM agent_impersonations WHERE agent_id=%s "
                "AND status IN ('requested','accepted','active') FOR UPDATE",
                (agent_id,),
            )
            lease = cur.fetchone()
        if lease is None:
            return "not_open"
        lease = parse_lease(lease)
        if lease["session_id"] != session_id:
            return "not_open"
        set_actor(conn, actor)
        if is_log_native(lease):
            close_event_admission(conn, str(lease["id"]))
        inbound_id = None
        if lease["status"] == ImpersonationStatus.ACTIVE and not lease["automatic"]:
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
    publish_inbound_wake(db, bus, agent_id, "impersonation-expired")
    publish_impersonation_changed_sync(bus, agent_id)
    publish_agent_updated_sync(bus, agent_id)
    return "expired"


# Leave at least five minutes for renewal on ordinary leases, and more on long
# leases. Short leases enter the window immediately; one reminder per deadline.
REMINDER_WINDOW_SECONDS = 300.0
REMINDER_TTL_FRACTION = 0.1


def remind_expiring_impersonations(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    *,
    window_seconds: float = REMINDER_WINDOW_SECONDS,
) -> int:
    """Insert one pending renewal reminder per approaching expiry deadline.

    Runs in the TTL reaper cycle (default 60s), ahead of expiry
    reconciliation. The lead time is max(10% of the current TTL,
    `window_seconds`), capped at the TTL itself. Short leases are eligible from
    activation; expired leases are left to the expiry pass. The reminder is an ordinary durable inbox row of
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
            "SELECT l.id, l.agent_id, l.expires_at, l.session_id "
            "FROM agent_impersonations l "
            "WHERE l.status='active' "
            "AND l.expires_at>clock_timestamp() "
            "AND l.expires_at<=clock_timestamp()+make_interval(secs=>"
            "LEAST(l.ttl_seconds,GREATEST(l.ttl_seconds*%s,%s))) "
            "AND NOT EXISTS (SELECT 1 FROM inbound_messages r "
            "WHERE r.agent_id=l.agent_id AND r.kind='reminder' "
            "AND r.payload->>'lease_id'=l.id::text "
            "AND (r.payload->>'expires_at')::timestamptz=l.expires_at) "
            "ORDER BY l.agent_id LIMIT %s",
            (REMINDER_TTL_FRACTION, window_seconds, _PASS_BATCH),
        ).fetchall()
        for lease_id, agent_id, expires_at, session_id in candidates:
            lease = lock_lease(conn, str(lease_id))
            if lease["status"] != ImpersonationStatus.ACTIVE or lease["expires_at"] != expires_at:
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
            # A bare `ava`: the host's `~/.local/bin/ava`, linked to the production CLI.
            prefix = ["ava", "impersonate"]
            # Suggest the window the executor last chose, not a fixed hour: the
            # guide asks for short leases extended in steps.
            ttl = str(lease["ttl_seconds"])
            renew = shlex.join(
                [*prefix, "renew", str(session_id), "--agent", str(agent_id), "--ttl", ttl]
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
        publish_inbound_wake(db, bus, agent_id, "impersonation-reminder")
    return len(reminded_agents)
