"""Cooperative same-machine leases; native execution remains the checkpoint owner.

No code is executed here. External Python processes bind their own SDK identity,
read/ack durable inbox rows and stage plugin state until native execution resumes.
"""

import secrets
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from shared import redis_client
from shared._impersonation_store import (
    OPEN,
    authenticate,
    authenticate_relay,
    expire,
    insert_handoff,
    local,
    lock_agent,
    lock_lease,
    public,
    require_active_locked,
    require_native,
    require_relay_active_locked,
    token_hash,
    validate_active,
    validate_relay_spec,
)
from shared._impersonation_store import (
    ImpersonationError as ImpersonationError,
)
from shared.caller_identity import CallerIdentity, caller_payload
from shared.config import settings
from shared.db import connect, publish_inbound_wake
from shared.db_transaction import write_transaction
from shared.live_events import Cancelled
from shared.log import logger
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation

RELAY_HEARTBEAT_SECONDS = 10.0
RELAY_HEARTBEAT_STALE_SECONDS = 45.0
_RELAY_FAILURE_STAMP_SECONDS = 600


def _ttl(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 86400:
        raise ValueError("TTL must be an integer from 1 through 86400 seconds")
    return value


def _wake(agent_id: int) -> None:
    publish_inbound_wake(agent_id, "impersonation")


def request(
    agent_id: int,
    *,
    caller: CallerIdentity,
    ttl_seconds: int = 3600,
    reason: str = "",
    relay_provider: str,
    relay_thread_id: str | None = None,
    relay_codex_remote: str | None = None,
) -> dict[str, Any]:
    """Ask the native agent for consent; return the secret once, never store it raw.

    Every request must name its relay endpoint up front: the accepting runtime
    never guesses one. A claude request also mints the scoped relay credential
    here (its relay runs inside the controller's own session); the codex
    credential is minted by the native side at activation instead.
    """
    ttl = _ttl(ttl_seconds)
    if caller.kind != "external_agent":
        raise ValueError("Impersonation requires an external_agent caller")
    validate_relay_spec(relay_provider, relay_thread_id, relay_codex_remote)
    relay_token = secrets.token_urlsafe(32) if relay_provider == "claude" else None
    lease_id, token = uuid4(), secrets.token_urlsafe(32)
    with write_transaction() as conn:
        meta = lock_agent(conn, agent_id)
        if meta["machine"] != machine_name():
            raise ImpersonationError("Impersonation is limited to the agent's own machine")
        if meta["status"] not in ("running", "idling"):
            raise ImpersonationError("Agent must be running or idling to receive a request")
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM agent_impersonations WHERE agent_id=%s "
                "AND (status IN ('requested','accepted','active') OR delta_version>applied_version) "
                "FOR UPDATE",
                (agent_id,),
            )
            previous = cur.fetchone()
        if previous is not None:
            previous = expire(conn, previous)
            if (
                previous["status"] in OPEN
                or previous["delta_version"] > previous["applied_version"]
            ):
                raise ImpersonationError("Agent already has a request, lease, or unapplied state")
        conn.execute(
            "INSERT INTO agent_impersonations(id,agent_id,source,machine,token_hash,reason,"
            "status,ttl_seconds,expires_at,relay_provider,relay_thread_id,relay_codex_remote,"
            "relay_token_hash) VALUES(%s,%s,%s,%s,%s,%s,'requested',%s,"
            "clock_timestamp()+%s*interval '1 second',%s,%s,%s,%s)",
            (
                lease_id,
                agent_id,
                caller.source(),
                meta["machine"],
                token_hash(token),
                reason,
                ttl,
                ttl,
                relay_provider,
                relay_thread_id,
                relay_codex_remote,
                token_hash(relay_token) if relay_token is not None else None,
            ),
        )
        result = public(lock_lease(conn, str(lease_id)))
    _wake(agent_id)
    result_with_token = result | {"token": token}
    if relay_token is not None:
        result_with_token["relay_token"] = relay_token
    return result_with_token


def get(lease_id: str, token: str) -> dict[str, Any]:
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        authenticate(lease, token)
        return public(expire(conn, lease))


def require_active(lease_id: str, token: str) -> dict[str, Any]:
    """Check one committed snapshot; SDK work does not hold a database lock."""
    with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT l.*,m.machine AS current_machine,m.status AS current_status,"
            "l.expires_at>clock_timestamp() AS fresh FROM agent_impersonations l "
            "JOIN agents_meta m ON m.id=l.agent_id WHERE l.id=%s",
            (lease_id,),
        )
        lease = cur.fetchone()
    if lease is None:
        raise ImpersonationError("Impersonation does not exist")
    validate_active(
        lease,
        token,
        fresh=lease.pop("fresh"),
        machine=lease.pop("current_machine"),
        status=lease.pop("current_status"),
    )
    return public(lease)


def accept(lease_id: str, agent_id: int, incarnation: RuntimeIncarnation) -> dict[str, Any]:
    if incarnation.agent_id != agent_id:
        raise ImpersonationError("Consent belongs to a different agent")
    with write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        local(lease)
        if lease["agent_id"] != agent_id or lease["status"] != "requested":
            raise ImpersonationError("Only the requested agent can accept a pending request")
        if lease["relay_provider"] is None:
            raise ImpersonationError(
                "Impersonation request has no relay binding; it cannot be accepted. "
                "Reject it and ask the controller to re-request with a relay endpoint."
            )
        if conn.execute("SELECT %s > clock_timestamp()", (lease["expires_at"],)).fetchone() != (
            True,
        ):
            raise ImpersonationError("Impersonation request has expired")
        conn.execute(
            "UPDATE agent_impersonations SET status='accepted',accepted_generation=%s,"
            "accepted_owner=%s WHERE id=%s",
            (incarnation.generation, incarnation.owner, lease_id),
        )
        result = public(lock_lease(conn, lease_id))
    _wake(agent_id)
    return result


def reject(
    lease_id: str, agent_id: int, incarnation: RuntimeIncarnation, reason: str = ""
) -> dict[str, Any]:
    with write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        if (
            incarnation.agent_id != agent_id
            or lease["agent_id"] != agent_id
            or lease["status"] != "requested"
        ):
            raise ImpersonationError("Only the requested agent can reject a pending request")
        conn.execute(
            "UPDATE agent_impersonations SET status='rejected',ended_at=clock_timestamp(),rejection_reason=%s "
            "WHERE id=%s",
            (reason, lease_id),
        )
        result = public(lock_lease(conn, lease_id))
    _wake(agent_id)
    return result


def activate(lease_id: str, incarnation: RuntimeIncarnation) -> dict[str, Any]:
    """Called only after native exec drains AND its checkpoint flush completes."""
    from shared.incarnation_resources import IncarnationResources, decode_resources

    with write_transaction() as conn:
        meta = require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        lease = expire(conn, lease)
        if lease["agent_id"] == incarnation.agent_id and lease["status"] == "expired":
            # Expiry between the driver's status read and this locked boundary
            # returns control; it is not a fatal native runtime failure.
            return public(lease)
        if lease["agent_id"] != incarnation.agent_id or lease["status"] != "accepted":
            raise ImpersonationError("Activation requires accepted native consent")
        if (lease["accepted_generation"], lease["accepted_owner"]) != (
            incarnation.generation,
            incarnation.owner,
        ):
            raise ImpersonationError("Activation belongs to another native incarnation")
        if meta["incarnation_resources"] is not None:
            resources = decode_resources(meta["incarnation_resources"])
            if not isinstance(resources, IncarnationResources) or resources.requests:
                raise ImpersonationError("Native execution resources have not drained")
        conn.execute(
            "UPDATE agent_impersonations SET status='active',activated_at=clock_timestamp(),"
            "expires_at=clock_timestamp()+ttl_seconds*interval '1 second' WHERE id=%s",
            (lease_id,),
        )
        result = public(lock_lease(conn, lease_id))
    _wake(incarnation.agent_id)
    return result


def native_status(agent_id: int, incarnation: RuntimeIncarnation) -> dict[str, Any] | None:
    if incarnation.agent_id != agent_id:
        raise ImpersonationError("Native status belongs to a different agent")
    # No-lease reads must not lock the hot native metadata row at every node.
    # A concurrently inserted request still needs this native owner's consent;
    # absence can delay presentation until the next gate, never activate it.
    with connect() as conn:
        row = conn.execute(
            "SELECT runtime_generation=%s AND runtime_owner=%s "
            "AND status IN ('running','idling') AND lease_expires_at>clock_timestamp(),"
            "EXISTS(SELECT 1 FROM agent_impersonations WHERE agent_id=%s AND "
            "(status IN ('requested','accepted','active') OR delta_version>applied_version)) "
            "FROM agents_meta WHERE id=%s",
            (incarnation.generation, incarnation.owner, agent_id, agent_id),
        ).fetchone()
    if row is None or row[0] is not True:
        raise ImpersonationError("Native runtime no longer owns this agent")
    if not row[1]:
        return None
    with write_transaction() as conn:
        require_native(conn, incarnation)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM agent_impersonations WHERE agent_id=%s AND "
                "(status IN ('requested','accepted','active') OR delta_version>applied_version) "
                "ORDER BY created_at LIMIT 1 FOR UPDATE",
                (agent_id,),
            )
            lease = cur.fetchone()
        if lease is None:
            return None
        lease = expire(conn, lease)
        if lease["status"] == "accepted" and (
            lease["accepted_generation"],
            lease["accepted_owner"],
        ) != (incarnation.generation, incarnation.owner):
            # A crash before the checkpoint ACK never transfers control. Ask
            # the replacement to make the decision again from its saved state.
            conn.execute(
                "UPDATE agent_impersonations SET status='requested',accepted_generation=NULL,"
                "accepted_owner=NULL,consent_version=consent_version+1 WHERE id=%s",
                (lease["id"],),
            )
            lease["status"] = "requested"
            lease["accepted_generation"] = None
            lease["accepted_owner"] = None
            lease["consent_version"] += 1
        return public(lease)


def renew(lease_id: str, token: str, *, ttl_seconds: int | None = None) -> dict[str, Any]:
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        require_active_locked(conn, lease, token)
        ttl = lease["ttl_seconds"] if ttl_seconds is None else _ttl(ttl_seconds)
        conn.execute(
            "UPDATE agent_impersonations SET ttl_seconds=%s,expires_at=clock_timestamp()+"
            "%s*interval '1 second' WHERE id=%s",
            (ttl, ttl, lease_id),
        )
        result = public(lock_lease(conn, lease_id))
    _wake(lease["agent_id"])
    return result


def release(lease_id: str, token: str, summary: str) -> dict[str, Any]:
    if not summary.strip():
        raise ValueError("A nonempty handoff summary is required")
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        authenticate(lease, token)
        if lease["status"] == "released":
            return public(lease)
        require_active_locked(conn, lease, token)
        inbound_id = insert_handoff(conn, lease, summary)
        conn.execute(
            "UPDATE agent_impersonations SET status='released',ended_at=clock_timestamp(),"
            "summary_inbound_id=%s WHERE id=%s",
            (inbound_id, lease_id),
        )
        result = public(lock_lease(conn, lease_id))
    _wake(lease["agent_id"])
    return result


def inbox(lease_id: str, token: str, *, limit: int = 100) -> list[dict[str, Any]]:
    if not 1 <= limit <= 1000:
        raise ValueError("Inbox limit must be from 1 through 1000")
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        require_active_locked(conn, lease, token)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id,content,kind,source,payload,created_at FROM inbound_messages "
                "WHERE agent_id=%s AND status='pending' AND kind IN ('chat','system_note','cancel') "
                "ORDER BY id LIMIT %s",
                (lease["agent_id"], limit),
            )
            messages = cur.fetchall()
        for message in messages:
            conn.execute(
                "INSERT INTO agent_impersonation_messages(lease_id,inbound_id) VALUES(%s,%s) "
                "ON CONFLICT DO NOTHING",
                (lease_id, message["id"]),
            )
    return messages


def ack(lease_id: str, token: str, message_ids: list[int]) -> None:
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        require_active_locked(conn, lease, token)
        rows = conn.execute(
            "SELECT inbound_id FROM agent_impersonation_messages WHERE lease_id=%s "
            "AND inbound_id=ANY(%s)",
            (lease_id, message_ids),
        ).fetchall()
        if {row[0] for row in rows} != set(message_ids):
            raise ImpersonationError("ACK contains messages not read by this impersonation")
        acknowledged = conn.execute(
            "UPDATE inbound_messages SET status='done' WHERE agent_id=%s AND id=ANY(%s) "
            "AND status='pending' RETURNING kind",
            (lease["agent_id"], message_ids),
        ).fetchall()
        conn.execute(
            "UPDATE agent_impersonation_messages SET acknowledged_at=clock_timestamp() "
            "WHERE lease_id=%s AND inbound_id=ANY(%s)",
            (lease_id, message_ids),
        )
    if any(row[0] == "cancel" for row in acknowledged):
        redis_client.publish_best_effort_sync(
            settings.data_plane.events_channel,
            Cancelled(agent_id=lease["agent_id"]).model_dump_json(),
            context="impersonation_cancel_ack",
        )
    # Consuming a page exposes previously hidden pending IDs to the relay.
    # Publish after real progress so its next page does not wait for DB catchup.
    if acknowledged:
        _wake(lease["agent_id"])


def merge_plugin_delta(
    lease_id: str, token: str, delta: dict[str, Any], *, expected_version: int
) -> None:
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        require_active_locked(conn, lease, token)
        if lease["delta_version"] != expected_version:
            raise ImpersonationError("Concurrent external state update; reload the agent state")
        conn.execute(
            "UPDATE agent_impersonations SET plugin_delta=plugin_delta || %s,"
            "delta_version=delta_version+1 WHERE id=%s",
            (Jsonb([delta]), lease_id),
        )


def mark_plugin_applied(lease_id: str, version: int, incarnation: RuntimeIncarnation) -> None:
    """Receipt follows a durable checkpoint containing the same lease/version."""
    with write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        if lease["agent_id"] != incarnation.agent_id or lease["status"] in OPEN:
            raise ImpersonationError("Plugin restoration requires the returned native agent")
        if not lease["applied_version"] <= version <= lease["delta_version"]:
            raise ImpersonationError("Invalid plugin state receipt version")
        conn.execute(
            "UPDATE agent_impersonations SET applied_version=%s WHERE id=%s",
            (version, lease_id),
        )


def relay_get(lease_id: str, relay_token: str) -> dict[str, Any]:
    """Lease reads for the bound relay process, under its scoped credential."""
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        authenticate_relay(lease, relay_token)
        return public(expire(conn, lease))


def relay_inbox(lease_id: str, relay_token: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """The relay's read-only inbox page: same durable rows, relay credential only.

    The controller token cannot read here and the relay token cannot release,
    renew or ACK anything — the handoff is scoped by construction.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("Inbox limit must be from 1 through 1000")
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        require_relay_active_locked(conn, lease, relay_token)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id,content,kind,source,payload,created_at FROM inbound_messages "
                "WHERE agent_id=%s AND status='pending' AND kind IN ('chat','system_note','cancel') "
                "ORDER BY id LIMIT %s",
                (lease["agent_id"], limit),
            )
            messages = cur.fetchall()
        for message in messages:
            conn.execute(
                "INSERT INTO agent_impersonation_messages(lease_id,inbound_id) VALUES(%s,%s) "
                "ON CONFLICT DO NOTHING",
                (lease_id, message["id"]),
            )
    return messages


def relay_heartbeat(lease_id: str, relay_token: str) -> None:
    """Fresh liveness evidence from the bound relay, while the lease is open."""
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        authenticate_relay(lease, relay_token)
        if lease["status"] not in OPEN:
            raise ImpersonationError("Impersonation has ended")
        conn.execute(
            "UPDATE agent_impersonations SET relay_heartbeat_at=clock_timestamp() WHERE id=%s",
            (lease_id,),
        )


def provision_relay(
    lease_id: str, incarnation: RuntimeIncarnation, relay_token: str
) -> dict[str, Any]:
    """Mint or re-mint the scoped relay credential on the lease row.

    Called by the accepting native runtime at activation (codex) and again
    when it respawns a dead relay. Re-provisioning revokes any earlier relay
    credential, so a slow-dying duplicate relay loses authority and exits.
    """
    with write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        if lease["agent_id"] != incarnation.agent_id or lease["status"] not in (
            "accepted",
            "active",
        ):
            raise ImpersonationError("Relay provisioning requires the native-held lease")
        if (lease["accepted_generation"], lease["accepted_owner"]) != (
            incarnation.generation,
            incarnation.owner,
        ):
            raise ImpersonationError("Relay provisioning belongs to the accepting incarnation")
        conn.execute(
            "UPDATE agent_impersonations SET relay_token_hash=%s WHERE id=%s",
            (token_hash(relay_token), lease_id),
        )
        return public(lock_lease(conn, lease_id))


def fail_acceptance(lease_id: str, incarnation: RuntimeIncarnation, reason: str) -> dict[str, Any]:
    """Relay establishment failed: the takeover does not stand.

    Terminal 'rejected' carries the reason; a system note tells the native
    agent its acceptance was rolled back and it keeps running. Only the
    accepting native runtime may fail its own acceptance.
    """
    if not reason.strip():
        raise ValueError("A nonempty failure reason is required")
    with write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        if lease["agent_id"] != incarnation.agent_id or lease["status"] != "accepted":
            raise ImpersonationError("Only the accepted native agent can fail relay establishment")
        if (lease["accepted_generation"], lease["accepted_owner"]) != (
            incarnation.generation,
            incarnation.owner,
        ):
            raise ImpersonationError("Acceptance belongs to another native incarnation")
        conn.execute(
            "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
            "VALUES(%s,%s,'system_note','system:impersonation',%s)",
            (
                lease["agent_id"],
                f"Impersonation takeover {lease['id']} was rolled back: {reason} "
                "You remain the native agent; no external controller was admitted.",
                Jsonb(
                    caller_payload("system:impersonation", {"impersonation_id": str(lease["id"])})
                ),
            ),
        )
        conn.execute(
            "UPDATE agent_impersonations SET status='rejected',ended_at=clock_timestamp(),"
            "rejection_reason=%s,relay_last_failure_at=clock_timestamp() WHERE id=%s",
            (reason, lease_id),
        )
        result = public(lock_lease(conn, lease_id))
    _wake(lease["agent_id"])
    logger.error(
        "impersonation relay establishment failed; takeover rolled back",
        agent_id=lease["agent_id"],
        lease_id=str(lease_id),
        reason=reason,
    )
    return result


def record_relay_failure(lease_id: str, incarnation: RuntimeIncarnation) -> bool:
    """Rate-limited durable stamp that relay supervision noticed a stale relay."""
    with write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        if lease["agent_id"] != incarnation.agent_id or lease["status"] != "active":
            raise ImpersonationError("Relay failure stamps require the active native-held lease")
        row = conn.execute(
            "UPDATE agent_impersonations SET relay_last_failure_at=clock_timestamp() "
            "WHERE id=%s AND (relay_last_failure_at IS NULL OR relay_last_failure_at < "
            "clock_timestamp() - %s*interval '1 second') RETURNING id",
            (lease_id, _RELAY_FAILURE_STAMP_SECONDS),
        ).fetchone()
    return row is not None


def relay_liveness_alert(agent_id: int) -> None:
    """Loud, best-effort signal when a wake lands for an agent whose active
    lease has a stale relay heartbeat. Never raises: the wake itself must not
    be held hostage to this diagnostic. Logged at most once per stamp interval.
    """
    from datetime import UTC, datetime, timedelta

    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT relay_provider,relay_heartbeat_at,relay_last_failure_at "
                "FROM agent_impersonations WHERE agent_id=%s AND status='active' "
                "ORDER BY created_at DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        if row is None:
            return
        provider, heartbeat, last_failure = row
        stale = heartbeat is None or heartbeat < datetime.now(UTC) - timedelta(
            seconds=RELAY_HEARTBEAT_STALE_SECONDS
        )
        recently_stamped = last_failure is not None and last_failure >= datetime.now(UTC) - (
            timedelta(seconds=_RELAY_FAILURE_STAMP_SECONDS)
        )
        if stale and not recently_stamped:
            logger.error(
                "inbound wake for an impersonated agent whose relay heartbeat is stale; "
                "messages may sit unread in the inbox",
                agent_id=agent_id,
                relay_provider=provider,
                relay_heartbeat_at=str(heartbeat),
            )
    except Exception:
        logger.exception("impersonation relay liveness check failed", agent_id=agent_id)
