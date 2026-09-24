"""Consume already collected SDK/business events; never instrument SDK calls.

The producer supplies the explicit agent/session association and stable event
identity. This boundary deliberately does not configure sampling or install
wrappers: the cluster-wide SDK event collector owns those responsibilities.
"""

import math
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from shared._impersonation_store import lock_lease
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.impersonation_history import append, event_belongs_to_agent, resolve


def _event_key(event_id: object) -> str:
    if not isinstance(event_id, (int, str)) or isinstance(event_id, bool):
        raise TypeError("SDK events require a stable event id")
    if event_id == "":
        raise ValueError("SDK event id cannot be empty")
    return f"event:{event_id}"


def _validate_event(event: dict[str, Any], lease: dict[str, Any]) -> datetime:
    if event["event_name"] == "sdk_call":
        _validate_sdk(event["attributes"])
    timestamp = event["ts"]
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp)
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        raise ValueError("SDK events require a timezone-aware timestamp")
    skew = settings.general.impersonation_event_clock_skew_guard_seconds
    if lease["activated_at"] is None or timestamp < lease["activated_at"] - timedelta(seconds=skew):
        raise ValueError("SDK event predates session activation")
    if lease["ended_at"] is not None and timestamp > lease["ended_at"] + timedelta(seconds=skew):
        raise ValueError("SDK event occurred after the session ended")
    return timestamp


def _validate_sdk(attributes: dict[str, Any]) -> None:
    if not isinstance(attributes["fn"], str) or not attributes["fn"]:
        raise ValueError("SDK events require a function name")
    duration = attributes["duration"]
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise ValueError("SDK events require a nonnegative duration")


def consume_events(agent_id: int, session_id: int, events: Iterable[dict[str, Any]]) -> int:
    """Persist original events exactly once under an explicit session binding.

    Each event uses the unified event shape: id, ts, agent_id, event_name,
    category, attributes (plus any original fields). Only sdk_call and audit
    events enter this handoff. SDK sampling is never extrapolated. Calls may
    arrive after release; the durable history still accepts them and refreshes
    the handoff export. Only an explicit upstream delivery manifest can certify
    complete accounting; neither an empty page nor native resumption does.
    """
    lease_id = str(resolve(agent_id, session_id)["id"])
    inserted = 0
    with write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        for event in events:
            if (
                lease["event_delivery_protocol_version"] == 1
                and event["attributes"].get("impersonation_session") != f"{agent_id}:{session_id}"
            ):
                raise ValueError(
                    "Protocol-v1 events require the immutable impersonation session tag"
                )
            if not event_belongs_to_agent(event, agent_id):
                raise ValueError("SDK/API event belongs to another agent")
            kind = "sdk_call" if event["event_name"] == "sdk_call" else "api_event"
            if kind == "api_event" and event["category"] != "audit":
                continue
            timestamp = _validate_event(event, lease)
            key = _event_key(event["id"])
            existing = conn.execute(
                "SELECT 1 FROM agent_impersonation_entries WHERE lease_id=%s AND event_key=%s",
                (lease_id, key),
            ).fetchone()
            if existing is None and lease["events_completed_at"] is not None:
                raise ValueError("Event arrived after certified delivery completion")
            normalized = {**event, "ts": timestamp.isoformat()}
            append(conn, lease_id, kind, normalized, event_key=key, created_at=timestamp)
            inserted += existing is None
        if inserted:
            conn.execute(
                "UPDATE agent_impersonations SET handoff_document=NULL WHERE id=%s",
                (lease_id,),
            )
            if lease["handoff_path"] is not None:
                from psycopg.types.json import Jsonb

                from shared.impersonation_history import export_handoff

                lease["handoff_document"] = None
                document, _ = export_handoff(lease, conn)
                conn.execute(
                    "UPDATE agent_impersonations SET handoff_document=%s WHERE id=%s",
                    (Jsonb(document), lease_id),
                )
    return inserted
