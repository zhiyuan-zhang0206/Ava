"""Short, agent-row-serialized transactions for cooperative impersonation."""

import hashlib
import hmac
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from shared.caller_identity import caller_payload
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation

OPEN = ("requested", "accepted", "active")


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


def lock_lease(conn: psycopg.Connection, lease_id: str) -> dict[str, Any]:
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
    return lease


def public(lease: dict[str, Any]) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, UUID) else value
        for key, value in lease.items()
        if key != "token_hash"
    }


def local(lease: dict[str, Any]) -> None:
    if lease["machine"] != machine_name():
        raise ImpersonationError("Impersonation is limited to the agent's own machine")


def authenticate(lease: dict[str, Any], token: str) -> None:
    if not hmac.compare_digest(lease["token_hash"], token_hash(token)):
        raise ImpersonationError("Invalid impersonation token")
    local(lease)


def require_native(conn: psycopg.Connection, incarnation: RuntimeIncarnation) -> dict[str, Any]:
    meta = lock_agent(conn, incarnation.agent_id)
    fresh = conn.execute(
        "SELECT lease_expires_at > clock_timestamp() FROM agents_meta WHERE id=%s",
        (incarnation.agent_id,),
    ).fetchone()
    if (
        (meta["runtime_generation"], meta["runtime_owner"])
        != (incarnation.generation, incarnation.owner)
        or meta["status"] not in ("running", "idling")
        or fresh != (True,)
    ):
        raise ImpersonationError("Native runtime no longer owns this agent")
    return meta


def insert_handoff(
    conn: psycopg.Connection, lease: dict[str, Any], content: str, *, expired: bool = False
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


def expire(conn: psycopg.Connection, lease: dict[str, Any]) -> dict[str, Any]:
    if lease["status"] not in OPEN:
        return lease
    fresh = conn.execute("SELECT %s > clock_timestamp()", (lease["expires_at"],)).fetchone()
    if fresh == (True,):
        return lease
    inbound_id = None
    if lease["status"] == "active":
        inbound_id = insert_handoff(
            conn,
            lease,
            f"Impersonation {lease['id']} by {lease['source']} expired. "
            "Control has returned. Unacknowledged messages remain pending; "
            "no external completion summary was supplied.",
            expired=True,
        )
    conn.execute(
        "UPDATE agent_impersonations SET status='expired',ended_at=clock_timestamp(), "
        "summary_inbound_id=%s WHERE id=%s",
        (inbound_id, lease["id"]),
    )
    lease["status"] = "expired"
    lease["summary_inbound_id"] = inbound_id
    return lease


def require_active_locked(conn: psycopg.Connection, lease: dict[str, Any], token: str) -> None:
    authenticate(lease, token)
    # Do not persist expiration here and then raise (which would roll it back).
    # The native boundary/get reconciler persists it independently.
    fresh = conn.execute("SELECT %s > clock_timestamp()", (lease["expires_at"],)).fetchone()
    if lease["status"] != "active" or fresh != (True,):
        raise ImpersonationError("Impersonation is not active or its TTL has expired")
    meta = lock_agent(conn, lease["agent_id"])
    if meta["machine"] != lease["machine"] or meta["status"] not in ("running", "idling"):
        raise ImpersonationError("Agent placement or lifecycle changed")
