"""Cooperative same-machine leases; the native agent remains the checkpoint owner."""

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
    dismiss_reminders,
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
from shared.config.service_read import current_field_values
from shared.db import connect, publish_inbound_wake
from shared.db_transaction import write_transaction
from shared.impersonation_history import append, capture_pending, set_actor
from shared.live_announce import publish_agent_updated_sync, publish_impersonation_changed_sync
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


def _wake(agent_id: int, *, roster_changed: bool = False) -> None:
    publish_inbound_wake(agent_id, "impersonation")
    publish_impersonation_changed_sync(agent_id)
    if roster_changed:
        publish_agent_updated_sync(agent_id)


def request(
    agent_id: int,
    *,
    caller: CallerIdentity,
    ttl_seconds: int = 3600,
    reason: str = "",
    relay_provider: str,
    relay_thread_id: str | None = None,
    relay_codex_remote: str | None = None,
    relay_batch_window_seconds: int = 0,
    name: str = "",
    executor_name: str = "",
    process_metadata: dict[str, Any] | None = None,
    automatic: bool = False,
) -> dict[str, Any]:
    """Prepare a controller lease and return its scoped relay credential."""
    ttl = _ttl(ttl_seconds)
    if automatic and (not name.strip() or not executor_name.strip()):
        raise ValueError("Session name and executor name must be nonempty")
    if caller.kind != "external_agent":
        raise ValueError("Impersonation requires an external_agent caller")
    validate_relay_spec(relay_provider, relay_thread_id, relay_codex_remote)
    if (
        not isinstance(relay_batch_window_seconds, int)
        or isinstance(relay_batch_window_seconds, bool)
        or not 0 <= relay_batch_window_seconds <= 300
    ):
        raise ValueError("relay_batch_window_seconds must be an integer from 0 through 300")
    relay_token = secrets.token_urlsafe(32) if relay_provider == "claude" else None
    lease_id = uuid4()
    delivery_config = current_field_values()
    event_delivery_protocol_version = _manifest_protocol_version(automatic=automatic)
    with write_transaction() as conn:
        meta = lock_agent(conn, agent_id)
        if meta["machine"] != machine_name():
            raise ImpersonationError("Impersonation is limited to the agent's own machine")
        if meta["status"] not in ("running", "idling"):
            raise ImpersonationError("Agent must be running or idling to receive a request")
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM agent_impersonations WHERE agent_id=%s "
                "AND (status IN ('requested','accepted','active') OR delta_version>applied_version OR (automatic AND handoff_applied_at IS NULL)) "
                "FOR UPDATE",
                (agent_id,),
            )
            previous = cur.fetchone()
        if previous is not None:
            previous = expire(conn, previous)
            if (
                previous["status"] in OPEN
                or previous["delta_version"] > previous["applied_version"]
                or (previous["automatic"] and previous["handoff_applied_at"] is None)
            ):
                raise ImpersonationError("Agent already has a request, lease, or unapplied state")
        set_actor(conn, caller.source())
        conn.execute(
            "INSERT INTO agent_impersonations(id,agent_id,source,machine,reason,"
            "status,ttl_seconds,expires_at,relay_provider,relay_thread_id,relay_codex_remote,"
            "relay_token_hash,relay_batch_window_seconds,name,executor_name,process_metadata,automatic,"
            "ack_window_seconds,max_delivery_attempts,event_delivery_protocol_version) "
            "VALUES(%s,%s,%s,%s,%s,'requested',%s,clock_timestamp()+%s*interval '1 second',"
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                lease_id,
                agent_id,
                caller.source(),
                meta["machine"],
                reason,
                ttl,
                ttl,
                relay_provider,
                relay_thread_id,
                relay_codex_remote,
                token_hash(relay_token) if relay_token is not None else None,
                relay_batch_window_seconds,
                name,
                executor_name or caller.source(),
                Jsonb(process_metadata or {}),
                automatic,
                delivery_config["impersonation_ack_window_seconds"],
                delivery_config["impersonation_max_delivery_attempts"],
                event_delivery_protocol_version,
            ),
        )
        result = public(lock_lease(conn, str(lease_id)))
    _wake(agent_id, roster_changed=True)
    if relay_token is not None:
        return result | {"relay_token": relay_token}
    return result


def _manifest_protocol_version(*, automatic: bool) -> int | None:
    """Admit v1 only for new automatic leases while the cluster gate is on."""
    return 1 if automatic and settings.general.impersonation_event_manifest_enabled else None


def get(lease_id: str, caller: object) -> dict[str, Any]:
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        authenticate(lease, caller)
        was_open = lease["status"] in OPEN
        result = public(expire(conn, lease))
    if was_open and result["status"] == "expired":
        _wake(lease["agent_id"], roster_changed=True)
    return result


def require_active(lease_id: str, caller: object) -> dict[str, Any]:
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
        caller,
        fresh=lease.pop("fresh"),
        machine=lease.pop("current_machine"),
        status=lease.pop("current_status"),
    )
    return public(lease)


def accept(
    lease_id: str,
    agent_id: int,
    incarnation: RuntimeIncarnation,
    start_message: str,
) -> dict[str, Any]:
    """Record native preparation and the required start message for the external session.

    The start message becomes the relay's first host message at activation, so
    the controller starts with the native agent's own words instead of a
    synthetic hint. An empty start message is rejected: user ruling 2026-09-08
    (decision C).
    """
    if not start_message.strip():
        raise ImpersonationError("A nonempty start message is required for the external controller")
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
            "accepted_owner=%s,start_message=%s WHERE id=%s",
            (incarnation.generation, incarnation.owner, start_message, lease_id),
        )
        if lease["event_delivery_protocol_version"] == 1:
            from shared.agents.impersonation_manifest import admit_certifier

            admit_certifier(conn, lease_id)
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
    _wake(agent_id, roster_changed=True)
    return result


def activate(lease_id: str, incarnation: RuntimeIncarnation) -> dict[str, Any]:
    """Called only after native exec drains AND its checkpoint flush completes."""
    from shared.incarnation_resources import IncarnationResources, decode_resources

    with write_transaction() as conn:
        meta = require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        was_open = lease["status"] in OPEN
        lease = expire(conn, lease)
        if lease["agent_id"] == incarnation.agent_id and lease["status"] == "expired":
            # Expiry between the driver's status read and this locked boundary
            # returns control; it is not a fatal native runtime failure.
            result = public(lease)
        else:
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
                    raise ImpersonationError("The native agent's resources have not drained")
            conn.execute(
                "UPDATE agent_impersonations SET status='active',activated_at=clock_timestamp(),"
                "expires_at=clock_timestamp()+ttl_seconds*interval '1 second' WHERE id=%s",
                (lease_id,),
            )
            result = public(lock_lease(conn, lease_id))
            capture_pending(conn, result)
    _wake(incarnation.agent_id, roster_changed=was_open and result["status"] == "expired")
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
            "(status IN ('requested','accepted','active') OR delta_version>applied_version OR (automatic AND handoff_applied_at IS NULL))) "
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
                "(status IN ('requested','accepted','active') OR delta_version>applied_version OR (automatic AND handoff_applied_at IS NULL)) "
                "ORDER BY created_at LIMIT 1 FOR UPDATE",
                (agent_id,),
            )
            lease = cur.fetchone()
        if lease is None:
            return None
        was_open = lease["status"] in OPEN
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
        if lease["status"] == "active" and (
            lease["accepted_generation"],
            lease["accepted_owner"],
        ) != (incarnation.generation, incarnation.owner):
            # Every hosted restart and host takeover mints a fresh incarnation
            # (restart NULLs runtime_generation/owner and admission re-mints,
            # agent/hosted_ownership.py). The active lease survives the
            # replacement, so its accepting-incarnation binding must follow the
            # native lineage — relay supervision re-provisions under the
            # current incarnation and provision_relay's strict check would
            # refuse a dead binding forever (task #2635's manual DB alignment).
            # Legitimacy is require_native above: the caller is the row's one
            # admitted incarnation. A lingering predecessor cannot race this
            # transfer — admission fences a foreign owner behind the previous
            # owner's lease expiry (a live host renews every beat), and every
            # lease mutation the old incarnation attempts dies at its own
            # require_native row check. The other accepted_* writer is hosted
            # admission itself (agent/hosted_ownership.align_accepting_binding,
            # issue #2052), which holds the same agents_meta row lock — after
            # it lands, this lazy sync is already a no-op.
            conn.execute(
                "UPDATE agent_impersonations SET accepted_generation=%s,accepted_owner=%s "
                "WHERE id=%s",
                (incarnation.generation, incarnation.owner, lease["id"]),
            )
            lease["accepted_generation"] = incarnation.generation
            lease["accepted_owner"] = incarnation.owner
            logger.info(
                "active lease accepting-incarnation binding inherited by the replacement runtime",
                agent_id=agent_id,
                lease_id=str(lease["id"]),
                generation=str(incarnation.generation),
            )
        result = public(lease)
    if was_open and result["status"] == "expired":
        _wake(agent_id, roster_changed=True)
    return result


def renew(lease_id: str, caller: object, *, ttl_seconds: int | None = None) -> dict[str, Any]:
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        require_active_locked(conn, lease, caller)
        set_actor(conn, lease["source"])
        ttl = lease["ttl_seconds"] if ttl_seconds is None else _ttl(ttl_seconds)
        conn.execute(
            "UPDATE agent_impersonations SET ttl_seconds=%s,expires_at=clock_timestamp()+"
            "%s*interval '1 second' WHERE id=%s",
            (ttl, ttl, lease_id),
        )
        result = public(lock_lease(conn, lease_id))
    _wake(lease["agent_id"])
    return result


def release(lease_id: str, caller: object, summary: str) -> dict[str, Any]:
    if not summary.strip():
        raise ValueError("A nonempty handoff summary is required")
    from shared.agents.impersonation_manifest import (
        close_manifest_admission,
        freeze_manifest,
        is_protocol_v1,
    )

    # The admission fence is durable even when a live participant delays the
    # release. Do not roll it back with the later freeze refusal.
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        authenticate(lease, caller)
        if lease["status"] == "released":
            return public(lease)
        require_active_locked(conn, lease, caller)
        set_actor(conn, lease["source"])
        if is_protocol_v1(lease):
            close_manifest_admission(conn, lease_id)

    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        authenticate(lease, caller)
        if lease["status"] == "released":
            return public(lease)
        require_active_locked(conn, lease, caller)
        set_actor(conn, lease["source"])
        if is_protocol_v1(lease):
            try:
                freeze_manifest(conn, lease)
            except RuntimeError as exc:
                raise ImpersonationError(
                    "Cannot release until every impersonation event participant seals"
                ) from exc
        inbound_id = (
            None
            if lease["automatic"]
            else insert_handoff(
                conn,
                lease,
                f"External session ended (lease {lease_id}).\n\n{summary}",
            )
        )
        conn.execute(
            "UPDATE agent_impersonations SET status='released',ended_at=clock_timestamp(),"
            "summary_inbound_id=%s,summary=%s WHERE id=%s",
            (inbound_id, summary, lease_id),
        )
        dismiss_reminders(conn, lease)
        result = public(lock_lease(conn, lease_id))
    _wake(lease["agent_id"], roster_changed=True)
    return result


def inbox(lease_id: str, caller: object, *, limit: int = 100) -> list[dict[str, Any]]:
    """Read and record the controller's pending inbox page, oldest first."""
    if not 1 <= limit <= 1000:
        raise ValueError("Inbox limit must be from 1 through 1000")
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        require_active_locked(conn, lease, caller)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id,content,kind,source,payload,created_at FROM inbound_messages "
                "WHERE agent_id=%s AND status='pending' AND kind IN "
                "('chat','system_note','cancel','reminder') "
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


def ack(lease_id: str, caller: object, message_ids: list[int]) -> None:
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        require_active_locked(conn, lease, caller)
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
        if message_ids:
            append(
                conn,
                lease_id,
                "lifecycle",
                {
                    "event": "ack",
                    "message_ids": sorted(set(message_ids)),
                    "source": lease["source"],
                },
                event_key="ack:" + ",".join(map(str, sorted(set(message_ids)))),
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
    lease_id: str, caller: object, delta: dict[str, Any], *, expected_version: int
) -> None:
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        require_active_locked(conn, lease, caller)
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
        was_open = lease["status"] in OPEN
        result = public(expire(conn, lease))
    if was_open and result["status"] == "expired":
        _wake(lease["agent_id"], roster_changed=True)
    return result


def relay_inbox(lease_id: str, relay_token: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """Read the relay's bounded inbox page and durable attempt state."""
    if not 1 <= limit <= 1000:
        raise ValueError("Inbox limit must be from 1 through 1000")
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        require_relay_active_locked(conn, lease, relay_token)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT i.id,i.content,i.kind,i.source,i.payload,i.created_at,"
                "COALESCE(m.delivery_attempts,0) AS delivery_attempts,"
                "(m.last_delivery_at IS NULL OR m.last_delivery_at <= "
                "clock_timestamp() - %s*interval '1 second') AS delivery_due "
                "FROM inbound_messages i LEFT JOIN agent_impersonation_messages m "
                "ON m.inbound_id=i.id AND m.lease_id=%s "
                "WHERE i.agent_id=%s AND i.status='pending' AND i.kind IN "
                "('chat','system_note','cancel','reminder') "
                "ORDER BY i.id LIMIT %s",
                (lease["ack_window_seconds"], lease_id, lease["agent_id"], limit),
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

    Called by the accepting native runtime at activation (codex) and again when
    a restart lost the in-memory handle inside its fresh-start window (task
    #3998). Re-provisioning revokes any earlier relay credential, so a
    slow-dying duplicate relay loses authority and exits. The durable mint mark
    (timestamp + minting incarnation, task #3998) is written in the same
    transaction; the supervisor reads it after a restart to tell an earlier
    incarnation's mint from "never minted".
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
            "UPDATE agent_impersonations SET relay_token_hash=%s,"
            "relay_minted_at=clock_timestamp(),relay_minted_generation=%s,"
            "relay_minted_owner=%s WHERE id=%s",
            (token_hash(relay_token), incarnation.generation, incarnation.owner, lease_id),
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
                    caller_payload(
                        "system:impersonation",
                        {"impersonation_id": str(lease["id"]), "note_tag": "impersonation"},
                    )
                ),
            ),
        )
        conn.execute(
            "UPDATE agent_impersonations SET status='rejected',ended_at=clock_timestamp(),"
            "rejection_reason=%s,relay_last_failure_at=clock_timestamp() WHERE id=%s",
            (reason, lease_id),
        )
        result = public(lock_lease(conn, lease_id))
    _wake(lease["agent_id"], roster_changed=True)
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


_ABORTED_REASON_PREFIX = "aborted: "


def aborted_detail(reason: object) -> str | None:
    """The component-death detail of a supervisor-aborted lease, else None."""
    if not isinstance(reason, str) or not reason.startswith(_ABORTED_REASON_PREFIX):
        return None
    return reason.removeprefix(_ABORTED_REASON_PREFIX).strip() or None


def abort_lease(
    lease_id: str, incarnation: RuntimeIncarnation, detail: str
) -> dict[str, Any] | None:
    """Stop a takeover whose core component died (task #3998), like a TTL expiry.

    ``detail`` is the plain death phrase ("the executor process is gone" / "the
    bound relay stopped heartbeating"). The lease goes terminal ("expired")
    with ``rejection_reason`` recorded as ``aborted: <detail>`` — the request's
    own ``reason`` (its stated purpose) is preserved, and the resume chain
    reads the prefixed marker back via ``aborted_detail``. A non-automatic
    active lease also gets the legacy end note (the automatic note is delivered
    by the resume chain), and pending renewal reminders are dismissed.
    Idempotent: an already-terminal lease returns None. Every writer's lease
    lock serializes with claim-time expiry, the TTL reaper and the terminate
    trigger, so an abort that loses the race is a no-op here.
    """
    detail = detail.strip()
    if not detail:
        raise ValueError("A nonempty abort detail is required")
    reason = f"{_ABORTED_REASON_PREFIX}{detail}"
    with write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        if lease["agent_id"] != incarnation.agent_id:
            raise ImpersonationError("Lease abort requires the native-held lease")
        if lease["status"] not in OPEN:
            return None
        inbound_id = None
        if lease["status"] == "active" and not lease["automatic"]:
            inbound_id = insert_handoff(
                conn,
                lease,
                f"Impersonation {lease['id']} by {lease['source']} stopped — {detail}. "
                "Control has returned; unacknowledged messages remain pending.",
                expired=True,
            )
        conn.execute(
            "UPDATE agent_impersonations SET status='expired',ended_at=clock_timestamp(),"
            "rejection_reason=%s,summary_inbound_id=%s WHERE id=%s",
            (reason, inbound_id, lease_id),
        )
        dismiss_reminders(conn, lease)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM agent_impersonations WHERE id=%s", (lease_id,))
            ended = cur.fetchone()
            assert ended is not None  # noqa: S101 — locked overhead row exists
    _wake(lease["agent_id"], roster_changed=True)
    return public(ended)


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
