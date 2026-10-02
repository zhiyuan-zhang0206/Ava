"""Log-native event recording (protocol v2): producers append event bodies to the lease's log.

The lease's own ``agent_impersonation_entries`` is the record. A source row is
appended in the caller's transaction (central producers) or at the capture seam
(a controller's receipt), and the database decides completeness from those rows
alone, so no external store is compared and no certifier takes part.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import psycopg
from psycopg.types.json import Jsonb

from base.agents.impersonation._store import lock_lease
from base.agents.impersonation.history import append, export_handoff
from base.telemetry import Event, event_id, event_line, event_line_digest
from base.telemetry.serialization import event_payload

LOG_PROTOCOL_VERSION = 2
# Cap on one lease's entries; reaching it fails capture rather than dropping a
# tail (about 1280 times the measured 78-event session maximum).
MAX_LOG_ENTRIES = 100_000
# Source key of rows appended by transaction-owning central producers.
CENTRAL_SOURCE = "central"


def is_log_native(lease: dict[str, Any]) -> bool:
    """Whether this lease records its events in its own entries (protocol v2)."""
    return (
        lease["automatic"] is True
        and lease["event_delivery_protocol_version"] == LOG_PROTOCOL_VERSION
    )


def event_item(event: Event) -> tuple[str, str, str, object]:
    """Return the event's stable key, full-byte digest, entry kind and timestamp."""
    line = event_line(event)
    timestamp_ns = int(event.ts.timestamp() * 1_000_000_000)
    key = f"event:{event_id(line, timestamp_ns)}"
    kind = "sdk_call" if event.event_name == "sdk_call" else "api_event"
    return key, event_line_digest(event), kind, event.ts


def locked_receipt_state(conn: psycopg.Connection, lease_id: str, source_key: str) -> str | None:
    """Lock one controller receipt and return its state (None when it does not exist)."""
    row = conn.execute(
        "SELECT lock_impersonation_event_participant(%s,%s)", (lease_id, source_key)
    ).fetchone()
    if row is None:
        raise RuntimeError("Receipt lock function returned no row")
    return cast(str | None, row[0])


def append_source_event(
    conn: psycopg.Connection, lease: dict[str, Any], event: Event, *, source_key: str
) -> None:
    """Record one event body in the lease's own log, in the caller's transaction."""
    if lease["next_entry"] >= MAX_LOG_ENTRIES:
        raise RuntimeError("Impersonation event log reached its entry cap")
    line = event_line(event)
    key, digest, kind, timestamp = event_item(event)
    timestamp_ns = int(event.ts.timestamp() * 1_000_000_000)
    payload = {**event_payload(event), "id": event_id(line, timestamp_ns), "line_sha256": digest}
    append(
        conn,
        str(lease["id"]),
        kind,
        payload,
        event_key=key,
        created_at=cast(datetime, timestamp),
        source_key=source_key,
    )


def refresh_completed_export(conn: psycopg.Connection, lease_id: str) -> None:
    """Rewrite an already delivered hand-off file once its event log completes."""
    lease = lock_lease(conn, lease_id)
    if lease["events_completed_at"] is not None and lease["handoff_path"] is not None:
        document, _ = export_handoff(lease, conn)
        conn.execute(
            "UPDATE agent_impersonations SET handoff_document=%s WHERE id=%s",
            (Jsonb(document), lease_id),
        )
