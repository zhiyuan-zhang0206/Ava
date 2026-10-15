"""Short, agent-row-serialized transactions for cooperative impersonation."""

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from base.agents.impersonation.status import (
    OPEN,
    ImpersonationStatus,
    LeaseRecord,
    is_log_native,
    parse_lease,
)
from base.api_contracts.agent_contract import AgentStatus
from base.api_contracts.caller_identity import caller_payload
from base.native_process.runtime_incarnation import RuntimeIncarnation


class ImpersonationError(RuntimeError):
    """The lease, local placement, or expected ownership does not permit work."""


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def lock_agent(conn: psycopg.Connection, agent_id: int) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise ImpersonationError(f"Agent {agent_id} does not exist")
    return row


def lock_lease(conn: psycopg.Connection, lease_id: str) -> LeaseRecord:
    # Read only the immutable foreign key first; all mutations acquire the agent
    # lock before the lease or inbox, matching native ownership and claim order.
    row = conn.execute(
        "SELECT agent_id FROM agent_impersonations WHERE id=%s", (UUID(str(lease_id)),)
    ).fetchone()
    if row is None:
        raise ImpersonationError("Impersonation does not exist")
    lock_agent(conn, row[0])
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM agent_impersonations WHERE id=%s FOR UPDATE", (lease_id,))
        lease = cur.fetchone()
    if lease is None:
        raise ImpersonationError("Impersonation disappeared")
    return parse_lease(lease)


def public(lease: Mapping[str, Any]) -> dict[str, Any]:
    status = ImpersonationStatus(lease["status"])
    return {
        key: status if key == "status" else str(value) if isinstance(value, UUID) else value
        for key, value in lease.items()
        if key not in ("token_hash", "relay_token_hash")
    }


def local(
    lease: Mapping[str, Any],
    *,
    local_machine: str,
) -> None:
    if lease["machine"] != local_machine:
        raise ImpersonationError("Impersonation is limited to the agent's own machine")


def authenticate(
    lease: Mapping[str, Any],
    *,
    local_machine: str,
) -> None:
    """Controller authority requires same-machine placement."""
    local(
        lease,
        local_machine=local_machine,
    )


def require_native(conn: psycopg.Connection, incarnation: RuntimeIncarnation) -> dict[str, Any]:
    meta = lock_agent(conn, incarnation.agent_id)
    fresh = conn.execute(
        "SELECT lease_expires_at > clock_timestamp() FROM agents_meta WHERE id=%s",
        (incarnation.agent_id,),
    ).fetchone()
    if (
        (meta["runtime_generation"], meta["runtime_owner"])
        != (incarnation.generation, incarnation.owner)
        or AgentStatus(meta["status"]) not in (AgentStatus.RUNNING, AgentStatus.IDLING)
        or fresh != (True,)
    ):
        raise ImpersonationError("Native runtime no longer owns this agent")
    return meta


def insert_handoff(
    conn: psycopg.Connection, lease: Mapping[str, Any], content: str, *, expired: bool = False
) -> int:
    """Write only the negotiated workflow's handoff, in the lease transaction.

    The native consumer has explicitly accepted this source through its private
    request table. This is not generic caller-protocol activation: all ordinary
    external message/lifecycle writers keep their existing rollout fences.
    """
    source = "system:impersonation" if expired else lease["source"]
    payload = caller_payload(source, {"impersonation_id": str(lease["id"])})
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
        "VALUES(%s,%s,'chat',%s,%s) RETURNING id",
        (lease["agent_id"], content, source, Jsonb(payload)),
    ).fetchone()
    if row is None:
        raise RuntimeError("Handoff INSERT returned no row")
    return row[0]


def dismiss_reminders(conn: psycopg.Connection, lease: Mapping[str, Any]) -> None:
    """Retire this lease's pending renewal reminders, in the lease transaction.

    Reminders are written for the external controller only; once the lease
    ends (release or expiry) they are meaningless to the native inbox. ACKed
    rows are already done. The payload key matches the maintenance insert.
    """
    conn.execute(
        "UPDATE inbound_messages SET status='done' "
        "WHERE agent_id=%s AND kind='reminder' AND status='pending' "
        "AND payload->>'lease_id'=%s",
        (lease["agent_id"], str(lease["id"])),
    )


def expire(conn: psycopg.Connection, lease: LeaseRecord) -> LeaseRecord:
    if lease["status"] not in OPEN:
        return lease
    fresh = conn.execute("SELECT %s > clock_timestamp()", (lease["expires_at"],)).fetchone()
    # Reconcile across the entire lease, not just the relay's bounded inbox
    # page. ACK and delivery reservations take this same agent/lease lock.
    overdue = (
        conn.execute(
            "SELECT m.inbound_id FROM agent_impersonation_messages m "
            "JOIN inbound_messages i ON i.id=m.inbound_id "
            "WHERE m.lease_id=%s AND m.acknowledged_at IS NULL AND i.status='pending' "
            "AND m.delivery_attempts >= %s "
            "AND m.last_delivery_at <= clock_timestamp() - %s*interval '1 second' "
            "ORDER BY m.inbound_id LIMIT 1",
            (lease["id"], lease["max_delivery_attempts"], lease["ack_window_seconds"]),
        ).fetchone()
        if lease["status"] == ImpersonationStatus.ACTIVE
        else None
    )
    if fresh == (True,):
        if overdue is not None:
            reason = f"message {overdue[0]} exhausted its delivery budget; receipt remains possible"
            conn.execute(
                "UPDATE agent_impersonations SET relay_degraded_reason=%s,"
                "relay_degraded_at=clock_timestamp() WHERE id=%s AND relay_degraded_reason IS DISTINCT FROM %s",
                (reason, lease["id"], reason),
            )
            lease["relay_degraded_reason"] = reason
        return lease
    if is_log_native(lease):
        close_event_admission(conn, str(lease["id"]))
    detail = None
    inbound_id = None
    if lease["status"] == ImpersonationStatus.ACTIVE and not lease["automatic"]:
        inbound_id = insert_handoff(
            conn,
            lease,
            f"Impersonation {lease['id']} by {lease['source']} "
            + (f"stopped: {detail}. " if detail else "expired. ")
            + "Control has returned. Unacknowledged messages remain pending; "
            "no external completion summary was supplied.",
            expired=True,
        )
    conn.execute(
        "UPDATE agent_impersonations SET status='expired',ended_at=clock_timestamp(), "
        "summary_inbound_id=%s,rejection_reason=COALESCE(%s,rejection_reason) WHERE id=%s",
        (inbound_id, f"aborted: {detail}" if detail else None, lease["id"]),
    )
    # A pending renewal reminder only matters to the external session; the
    # lease is over, so it must never reach the native agent's inbox.
    dismiss_reminders(conn, lease)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM agent_impersonations WHERE id=%s", (lease["id"],))
        refreshed = cur.fetchone()
        assert refreshed is not None  # noqa: S101 - locked session exists
        lease = parse_lease(refreshed)
    lease["status"] = ImpersonationStatus.EXPIRED
    lease["summary_inbound_id"] = inbound_id
    return lease


def validate_active(
    lease: LeaseRecord,
    *,
    fresh: bool,
    machine: str,
    status: AgentStatus,
    local_machine: str,
) -> None:
    """Validate either a joined read snapshot or rows already locked for mutation."""
    if lease["status"] != ImpersonationStatus.ACTIVE or not fresh:
        raise ImpersonationError(
            "Impersonation session is not active (stale-session): it ended or its "
            "TTL expired; the native agent continues."
        )
    if machine != lease["machine"] or status not in (AgentStatus.RUNNING, AgentStatus.IDLING):
        raise ImpersonationError("Agent placement or lifecycle changed")
    authenticate(
        lease,
        local_machine=local_machine,
    )


def require_active_locked(
    conn: psycopg.Connection,
    lease: LeaseRecord,
    *,
    local_machine: str,
) -> None:
    # Do not persist expiration here and then raise (which would roll it back).
    # The native boundary/get reconciler persists it independently.
    meta = lock_agent(conn, lease["agent_id"])
    fresh = conn.execute("SELECT %s > clock_timestamp()", (lease["expires_at"],)).fetchone()
    validate_active(
        lease,
        fresh=fresh == (True,),
        machine=meta["machine"],
        status=AgentStatus(meta["status"]),
        local_machine=local_machine,
    )


RELAY_PROVIDERS = ("codex", "claude", "dsh", "antigravity")
# Relays that run inside the controller's own session: the request mints their
# scoped credential, the activation gate waits for their heartbeat, and the
# native side never spawns or re-provisions them. codex and antigravity relays
# are spawned by the accepting runtime instead.
SESSION_RELAY_PROVIDERS = ("claude", "dsh")


def validate_relay_spec(
    provider: str | None, thread_id: str | None, codex_remote: str | None
) -> None:
    """The request must name its relay endpoint up front; the native side
    never guesses one. A codex remote must be a unix:// or ws:// address,
    rejected here so a malformed endpoint fails the request instead of the
    relay (review N4)."""
    if provider not in RELAY_PROVIDERS:
        raise ValueError("Relay provider must be 'codex', 'claude', 'dsh' or 'antigravity'")
    if provider == "codex":
        if not thread_id:
            raise ValueError("Codex relay requires the existing session's thread id")
        if codex_remote is not None and not codex_remote.startswith(("unix://", "ws://")):
            raise ValueError("Codex remote must be a unix:// or ws:// endpoint")
    elif provider == "antigravity":
        if not thread_id or not thread_id.strip():
            raise ValueError("Antigravity relay requires the conversation's thread id")
        if codex_remote is not None:
            raise ValueError("The antigravity relay does not use --codex-remote")
    elif thread_id is not None or codex_remote is not None:
        raise ValueError(
            f"The {provider} relay routes to its owner; thread id and remote are rejected"
        )


def authenticate_relay(
    lease: Mapping[str, Any],
    relay_token: str,
    *,
    local_machine: str,
) -> None:
    """The relay's scoped credential: inbox/delivery/beat, never controller authority."""
    if lease["relay_token_hash"] is None or not hmac.compare_digest(
        lease["relay_token_hash"], token_hash(relay_token)
    ):
        raise ImpersonationError("Invalid relay token")
    local(
        lease,
        local_machine=local_machine,
    )


def require_relay_active_locked(
    conn: psycopg.Connection,
    lease: LeaseRecord,
    relay_token: str,
    *,
    local_machine: str,
) -> None:
    meta = lock_agent(conn, lease["agent_id"])
    fresh = conn.execute("SELECT %s > clock_timestamp()", (lease["expires_at"],)).fetchone()
    authenticate_relay(
        lease,
        relay_token,
        local_machine=local_machine,
    )
    if lease["status"] != ImpersonationStatus.ACTIVE or fresh != (True,):
        raise ImpersonationError("Impersonation is not active or its TTL has expired")
    if meta["machine"] != lease["machine"] or AgentStatus(meta["status"]) not in (
        AgentStatus.RUNNING,
        AgentStatus.IDLING,
    ):
        raise ImpersonationError("Agent placement or lifecycle changed")


def close_event_admission(conn: psycopg.Connection, lease_id: str) -> bool:
    """Close a log-native lease's admission gate through its narrow SQL door."""
    row = conn.execute(
        "SELECT close_impersonation_event_admission(%s)",
        (lease_id,),
    ).fetchone()
    return row is not None and row[0] is True
