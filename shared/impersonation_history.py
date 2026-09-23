"""Permanent session history and structured session-record files.

The UUID is a private reference retained for pre-upgrade checkpoint receipts.
Every public handle, path and display uses the agent-scoped integer session id.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from shared.db import connect
from shared.db_transaction import write_transaction
from shared.machine import machine_name
from shared.paths import workspace_dir
from shared.private_storage import write_private_bytes


class ImpersonationMetadata(BaseModel):
    """Declared identity and observed process facts on a rendered message."""

    agent_id: int
    session_id: int
    name: str
    executor_name: str
    provider: str | None
    process: dict[str, Any]
    anchor_item_id: str | None = None
    seq: int | None = None


def metadata(lease: dict[str, Any]) -> ImpersonationMetadata:
    return ImpersonationMetadata(
        agent_id=lease["agent_id"],
        session_id=lease["session_id"],
        name=lease["name"],
        executor_name=lease["executor_name"],
        provider=lease["relay_provider"],
        process=lease["process_metadata"],
    )


def public_session(lease: dict[str, Any]) -> dict[str, Any]:
    """Expose the numeric handle without legacy UUIDs or credentials."""
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
        "rejection_reason",
        "relay_provider",
        "relay_heartbeat_at",
        "relay_last_failure_at",
        "events_completed_at",
    )
    result = {key: lease[key] for key in fields} | {"id": lease["session_id"]}
    if lease["automatic"] and result["status"] in ("requested", "accepted"):
        result["status"] = "preparing"
    return result


def resolve(agent_id: int, session_id: int) -> dict[str, Any]:
    if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id < 0:
        raise ValueError("session_id must be a nonnegative integer")
    with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM agent_impersonations WHERE agent_id=%s AND session_id=%s",
            (agent_id, session_id),
        )
        lease = cur.fetchone()
    if lease is None:
        raise ValueError(f"Agent {agent_id} has no impersonation session {session_id}")
    if lease["machine"] != machine_name():
        raise ValueError("Impersonation operations must run on the agent's machine")
    return lease


def set_actor(conn: psycopg.Connection, actor: str) -> None:
    conn.execute("SELECT set_config('ava.impersonation_actor',%s,true)", (actor,))


def append(
    conn: psycopg.Connection,
    lease_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    event_key: str | None = None,
    created_at: datetime | None = None,
) -> int:
    """Append under the lease lock; repeat event identities return the original row."""
    if event_key is not None:
        existing = conn.execute(
            "SELECT seq,payload FROM agent_impersonation_entries WHERE lease_id=%s AND event_key=%s",
            (lease_id, event_key),
        ).fetchone()
        if existing is not None:
            if existing[1] != payload:
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
        "INSERT INTO agent_impersonation_entries(lease_id,seq,kind,event_key,created_at,payload) "
        "VALUES(%s,%s,%s,%s,COALESCE(%s,clock_timestamp()),%s)",
        (lease_id, seq, kind, event_key, created_at, Jsonb(payload)),
    )
    return seq


def capture_pending(conn: psycopg.Connection, lease: dict[str, Any]) -> None:
    """Include the backlog handed to this session, even if it predates activation."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id,content,kind,source,payload,created_at FROM inbound_messages "
            "WHERE agent_id=%s AND status='pending' AND kind IN ('chat','system_note','cancel','reminder') "
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
    lease_id: str,
    caller: object,
    content: str,
    *,
    phase: str = "commentary",
    message_key: str,
) -> int:
    """Commit a user-visible reply before publishing its refresh notification."""
    from shared._impersonation_store import lock_lease, require_active_locked
    from shared.config import settings
    from shared.live_events import ImpersonationChanged
    from shared.redis_client import publish_best_effort_sync

    if not content.strip() or phase not in ("commentary", "final") or not message_key.strip():
        raise ValueError("A message needs nonempty content/key and commentary or final phase")
    with write_transaction() as conn:
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
    publish_best_effort_sync(
        settings.data_plane.events_channel,
        ImpersonationChanged(agent_id=lease["agent_id"]).model_dump_json(),
        context="impersonation_message",
    )
    return seq


def entries(lease_id: str, conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT seq,kind,created_at,payload FROM agent_impersonation_entries "
            "WHERE lease_id=%s ORDER BY seq",
            (lease_id,),
        )
        return cur.fetchall()


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
        "sdk_sampled": any(item.get("sample_rate", 1) != 1 for item in attributes),
        "sdk_duration_seconds": sum(item["duration"] for item in attributes),
    }


def _event_delivery_statistics(
    lease: dict[str, Any], sdk: list[dict[str, Any]], api: list[dict[str, Any]]
) -> dict[str, Any]:
    """Describe whether observed handoff events are a complete session census.

    Only `complete_delivery()` accepts the upstream manifest that can certify
    coverage. Before that receipt, zero consumed events is an unknown result,
    never evidence that the external controller made no calls.
    """
    complete = lease["events_completed_at"] is not None
    coverage = "complete" if complete else "unknown"
    return {
        "state": "complete" if complete else "pending",
        "completion_basis": "upstream_manifest" if complete else None,
        "sdk_calls": {"coverage": coverage, "consumed_event_count": len(sdk)},
        "api_events": {"coverage": coverage, "consumed_event_count": len(api)},
    }


def build_document(lease: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive counts only from recorded facts; preserve the original events too."""
    sdk = [row for row in rows if row["kind"] == "sdk_call"]
    api = [row for row in rows if row["kind"] == "api_event"]
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


def export_handoff(lease: dict[str, Any], conn: psycopg.Connection) -> tuple[dict[str, Any], str]:
    """Write one JSON file atomically in the agent workspace, before native resumption."""
    document = lease["handoff_document"] or build_document(lease, entries(str(lease["id"]), conn))
    target = workspace_dir(lease["agent_id"]) / "impersonation" / f"{lease['session_id']}.json"
    document["session"]["handoff_path"] = str(target)
    encoded = json.dumps(document, ensure_ascii=False, indent=2, default=_json_default) + "\n"
    write_private_bytes(target, encoded.encode("utf-8"))
    return json.loads(encoded), str(target)
