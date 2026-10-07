"""Permanent session history and structured session-record files.

The UUID is a private reference retained for pre-upgrade checkpoint receipts.
Every public handle, path and display uses the agent-scoped integer session id.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from base.agents.impersonation.status import ImpersonationStatus, parse_lease
from base.cluster.machine import machine_name
from base.db import Database
from base.events.live.bus import EventBus
from base.host.private_storage import write_private_bytes
from base.paths import workspace_dir

# A consumed event's durable identity is content parsed from the original log line.
# Reader-synthesized projections (event id, line_sha256, tier, or future additions)
# stay outside this tuple; add a field only when it becomes producer content.
_EVENT_CONTENT_FIELDS: tuple[str, ...] = (
    "ts",
    "trace_id",
    "span_id",
    "agent_id",
    "machine",
    "process",
    "category",
    "event_name",
    "level",
    "source",
    "target_agent_id",
    "attributes",
)
_CONSUMED_EVENT_KINDS: tuple[str, ...] = ("sdk_call", "api_event")


class ImpersonationMetadata(BaseModel):
    """Declared identity on a rendered message.

    The observed process facts are captured once at request time and live on
    the session record (``process_metadata``), not on every message.
    """

    agent_id: int
    session_id: int
    name: str
    executor_name: str
    provider: str | None
    anchor_item_id: str | None = None
    seq: int | None = None


def metadata(lease: Mapping[str, Any]) -> ImpersonationMetadata:
    return ImpersonationMetadata(
        agent_id=lease["agent_id"],
        session_id=lease["session_id"],
        name=lease["name"],
        executor_name=lease["executor_name"],
        provider=lease["relay_provider"],
    )


def public_session(lease: Mapping[str, Any]) -> dict[str, Any]:
    """Expose the numeric handle without legacy UUIDs or credentials."""
    status = ImpersonationStatus(lease["status"])
    fields = (
        "agent_id",
        "session_id",
        "name",
        "executor_name",
        "process_metadata",
        "status",
        "created_at",
        "activated_at",
        "ended_at",
        "expires_at",
        "ttl_seconds",
        "ack_window_seconds",
        "max_delivery_attempts",
        "reason",
        "summary",
        "handoff_path",
        "handoff_applied_at",
        "rejection_reason",
        "relay_provider",
        "relay_heartbeat_at",
        "relay_last_failure_at",
        "events_completed_at",
    )
    result = {key: lease[key] for key in fields} | {"id": lease["session_id"]}
    result["status"] = status
    if lease["automatic"] and status in (
        ImpersonationStatus.REQUESTED,
        ImpersonationStatus.ACCEPTED,
    ):
        result["status"] = "preparing"
    return result


def resolve(db: Database, agent_id: int, session_id: int) -> dict[str, Any]:
    if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id < 0:
        raise ValueError("session_id must be a nonnegative integer")
    with db.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM agent_impersonations WHERE agent_id=%s AND session_id=%s",
            (agent_id, session_id),
        )
        lease = cur.fetchone()
    if lease is None:
        raise ValueError(f"Agent {agent_id} has no impersonation session {session_id}")
    parse_lease(lease)
    if lease["machine"] != machine_name():
        raise ValueError("Impersonation operations must run on the agent's machine")
    return lease


def set_actor(conn: psycopg.Connection, actor: str) -> None:
    conn.execute("SELECT set_config('ava.impersonation_actor',%s,true)", (actor,))


def _event_content(payload: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(payload.get(field) for field in _EVENT_CONTENT_FIELDS)


def append(
    conn: psycopg.Connection,
    lease_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    event_key: str | None = None,
    created_at: datetime | None = None,
    source_key: str | None = None,
) -> int:
    """Append under the lease lock; repeat event identities return the original row."""
    if event_key is not None:
        existing = conn.execute(
            "SELECT seq,payload FROM agent_impersonation_entries WHERE lease_id=%s AND event_key=%s",
            (lease_id, event_key),
        ).fetchone()
        if existing is not None:
            same_content = (
                _event_content(_resolve_payload(conn, existing[1]))
                == _event_content(_resolve_payload(conn, payload))
                if kind in _CONSUMED_EVENT_KINDS
                else existing[1] == payload
            )
            if not same_content:
                raise ValueError("Event key already belongs to different content")
            return int(existing[0])
    row = conn.execute(
        "UPDATE agent_impersonations SET next_entry=next_entry+1 WHERE id=%s "
        "RETURNING next_entry-1",
        (lease_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Impersonation session does not exist")
    seq = int(row[0])
    conn.execute(
        "INSERT INTO agent_impersonation_entries(lease_id,seq,kind,event_key,created_at,payload,"
        "source_key) VALUES(%s,%s,%s,%s,COALESCE(%s,clock_timestamp()),%s,%s)",
        (lease_id, seq, kind, event_key, created_at, Jsonb(payload), source_key),
    )
    return seq


def capture_pending(conn: psycopg.Connection, lease: Mapping[str, Any]) -> None:
    """Include the backlog handed to this session, even if it predates activation."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id,content,kind,source,payload,created_at FROM inbound_messages "
            "WHERE agent_id=%s AND status='pending' AND kind IN ('chat','system_note','cancel','reminder','heartbeat') "
            "ORDER BY id",
            (lease["agent_id"],),
        )
        rows = cur.fetchall()
    for row in rows:
        append(
            conn,
            str(lease["id"]),
            "message",
            {
                "direction": "in",
                "inbound_id": row["id"],
                "kind": row["kind"],
                "source": row["source"],
                "content": row["content"],
                "payload": row["payload"],
            },
            event_key=f"inbound:{row['id']}",
            created_at=row["created_at"],
        )


def say(
    db: Database,
    bus: EventBus,
    lease_id: str,
    caller: object,
    content: str,
    *,
    phase: str = "commentary",
    message_key: str,
) -> int:
    """Commit a user-visible reply before publishing its refresh notification."""
    from base.agents.impersonation._store import lock_lease, require_active_locked
    from base.events.live.projection import ImpersonationChanged

    if not content.strip() or phase not in ("commentary", "final") or not message_key.strip():
        raise ValueError("A message needs nonempty content/key and commentary or final phase")
    with db.write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        require_active_locked(conn, lease, caller)
        seq = append(
            conn,
            lease_id,
            "message",
            {
                "direction": "out",
                "source": f"agent:{lease['agent_id']}",
                "content": content,
                "phase": phase,
                "impersonation": metadata(lease).model_dump(),
            },
            event_key=f"reply:{message_key}",
        )
        if seq >= lease["next_entry"]:
            conn.execute(
                "UPDATE agents_meta SET last_message_text=%s,last_active_at=clock_timestamp() WHERE id=%s",
                (content, lease["agent_id"]),
            )
    bus.publish_best_effort_sync(
        ImpersonationChanged(agent_id=lease["agent_id"]).model_dump_json(),
        context="impersonation_message",
    )
    return seq


def entries(lease_id: str, conn: psycopg.Connection) -> list[dict[str, Any]]:
    """The lease's log in order, central event references resolved to their bodies."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT seq,kind,created_at,payload FROM agent_impersonation_entries "
            "WHERE lease_id=%s ORDER BY seq",
            (lease_id,),
        )
        rows = cur.fetchall()
    _resolve_event_references(rows, conn)
    return rows


def _audit_bodies(conn: psycopg.Connection, uids: list[int]) -> dict[int, dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT event_uid,ts,trace_id,span_id,agent_id,machine,process,event_name,level,"
            "source,target_agent_id,attributes FROM audit_events WHERE event_uid = ANY(%s)",
            (uids,),
        )
        return {body["event_uid"]: body for body in cur.fetchall()}


def _resolved_payload(reference: dict[str, Any], body: dict[str, Any] | None) -> dict[str, Any]:
    """The event payload a central reference names; a missing row is a broken invariant.

    The record is written in the same transaction as the reference, so it is
    never legitimately absent.
    """
    if body is None:
        raise RuntimeError(f"audit_events has no row for event_uid {reference['event_uid']}")
    return {
        "ts": body["ts"].astimezone(UTC).isoformat(),
        "trace_id": body["trace_id"],
        "span_id": body["span_id"],
        "agent_id": body["agent_id"],
        "machine": body["machine"],
        "process": body["process"],
        "category": "audit",
        "event_name": body["event_name"],
        "level": body["level"],
        "source": body["source"],
        "target_agent_id": body["target_agent_id"],
        "attributes": body["attributes"],
        "id": reference["id"],
        "line_sha256": reference["line_sha256"],
        "event_uid": reference["event_uid"],
    }


def _resolve_event_references(rows: list[dict[str, Any]], conn: psycopg.Connection) -> None:
    """Replace each central reference payload with the `audit_events` body it names."""
    refs = [row for row in rows if "event_uid" in row["payload"]]
    if not refs:
        return
    bodies = _audit_bodies(conn, [row["payload"]["event_uid"] for row in refs])
    for row in refs:
        row["payload"] = _resolved_payload(row["payload"], bodies.get(row["payload"]["event_uid"]))


def _resolve_payload(conn: psycopg.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    if "event_uid" not in payload:
        return payload
    return _resolved_payload(
        payload, _audit_bodies(conn, [payload["event_uid"]]).get(payload["event_uid"])
    )


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, UUID, Path)):
        return str(value) if not isinstance(value, datetime) else value.isoformat()
    raise TypeError(f"Unsupported history value: {type(value).__name__}")


def event_belongs_to_agent(event: dict[str, Any], agent_id: int) -> bool:
    """Audit source is the actor; send_message's primary agent is its recipient."""
    if event["event_name"] == "sdk_call":
        return event["agent_id"] == agent_id
    source = event["source"]
    return source == f"agent:{agent_id}" or (source == "self" and event["agent_id"] == agent_id)


def _messages_with_ack(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    acknowledged = {
        message_id
        for row in rows
        if row["kind"] == "lifecycle" and row["payload"].get("event") == "ack"
        for message_id in row["payload"]["message_ids"]
    }
    messages: list[dict[str, Any]] = []
    for row in rows:
        if row["kind"] != "message":
            continue
        message = dict(row)
        if row["payload"]["direction"] == "in":
            message["acknowledged"] = (
                row["payload"]["inbound_id"] in acknowledged
                or row["payload"].get("acknowledged_at") is not None
            )
        messages.append(message)
    return messages


def _api_statistics(api: list[dict[str, Any]], agent_id: int) -> dict[str, Any]:
    own = [row["payload"] for row in api if event_belongs_to_agent(row["payload"], agent_id)]
    counts = Counter(event["event_name"] for event in own)
    recipients = Counter(
        str(event["agent_id"]) for event in own if event["event_name"] == "send_message"
    )
    tasks = {
        operation: sorted(
            {event["attributes"]["task_id"] for event in own if event["event_name"] == operation}
        )
        for operation in ("task_create", "task_update")
    }
    return {
        "api_operations": dict(sorted(counts.items())),
        "message_recipients": dict(sorted(recipients.items())),
        "task_ids_created": tasks["task_create"],
        "task_ids_updated": tasks["task_update"],
        "tasks_created": len(tasks["task_create"]),
        "tasks_updated": len(tasks["task_update"]),
    }


def _sdk_statistics(sdk: list[dict[str, Any]]) -> dict[str, Any]:
    attributes = [row["payload"]["attributes"] for row in sdk]
    calls = Counter(item["fn"] for item in attributes)
    return {
        "sdk_calls": dict(sorted(calls.items())),
        "sdk_statistics_basis": "consumed_events",
        "sdk_sampling_policy": "unknown",
        "sdk_duration_seconds": sum(item["duration"] for item in attributes),
    }


def _event_delivery_statistics(
    lease: Mapping[str, Any], sdk: list[dict[str, Any]], api: list[dict[str, Any]]
) -> dict[str, Any]:
    """Describe whether the handoff's event log is complete for the emitted events.

    The database completes a log-native lease once it has ended and every source has
    sealed. SDK sampling policy is not certified per session, so zero
    consumed SDK events is never evidence of zero SDK calls.
    """
    complete = lease["events_completed_at"] is not None
    coverage = "complete_emitted_events" if complete else "unknown"
    return {
        "state": "complete" if complete else "pending",
        "pending_reason": None if complete else _pending_delivery_reason(lease),
        "completion_basis": "source_log" if complete else None,
        "sdk_calls": {
            "coverage": coverage,
            "sampling_policy": "unknown",
            "consumed_event_count": len(sdk),
        },
        "api_events": {"coverage": coverage, "consumed_event_count": len(api)},
    }


def _pending_delivery_reason(lease: Mapping[str, Any]) -> str:
    from base.agents.impersonation.manifest import pending_reason

    result = pending_reason(lease)
    if result is None:
        raise RuntimeError("A pending delivery must have a diagnostic reason")
    return result


def _by_occurrence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The log returns events in append order (late arrivals after);
    the handoff reader needs call order. ``created_at`` is the event's own time."""
    return sorted(rows, key=lambda row: (row["created_at"], row["seq"]))


def build_document(lease: Mapping[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive counts only from recorded facts; preserve the original events too."""
    sdk = _by_occurrence([row for row in rows if row["kind"] == "sdk_call"])
    api = _by_occurrence([row for row in rows if row["kind"] == "api_event"])
    messages = _messages_with_ack(rows)
    directions = Counter(row["payload"]["direction"] for row in messages)
    started, ended = lease["activated_at"] or lease["created_at"], lease["ended_at"]
    return {
        "version": 2,
        "session": public_session(lease),
        "messages": messages,
        "lifecycle": [row for row in rows if row["kind"] == "lifecycle"],
        "sdk_events": sdk,
        "api_events": api,
        "statistics": {
            **_sdk_statistics(sdk),
            **_api_statistics(api, lease["agent_id"]),
            "duration_seconds": (ended - started).total_seconds() if ended is not None else None,
            "event_delivery": _event_delivery_statistics(lease, sdk, api),
            "incoming_messages": directions["in"],
            "outgoing_messages": directions["out"],
        },
    }


def export_handoff(
    lease: Mapping[str, Any], conn: psycopg.Connection
) -> tuple[dict[str, Any], str]:
    """Write one JSON file atomically in the agent workspace, before native resumption."""
    document = lease["handoff_document"]
    if document is None or document["version"] != 2:
        document = build_document(lease, entries(str(lease["id"]), conn))
    target = workspace_dir(lease["agent_id"]) / "impersonation" / f"{lease['session_id']}.json"
    document["session"]["handoff_path"] = str(target)
    encoded = json.dumps(document, ensure_ascii=False, indent=2, default=_json_default) + "\n"
    write_private_bytes(target, encoded.encode("utf-8"))
    return json.loads(encoded), str(target)
