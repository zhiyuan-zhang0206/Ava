"""State alerts for impersonation event logs that cannot complete on their own.

Both conditions are facts about rows, so there is no threshold: an ended lease
still waiting on an open source (`ImpersonationEventSealStuck`, resolved when the
source seals) and a source whose capture failed (`ImpersonationEventCaptureFailed`,
permanent: a failed source never completes its lease).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

from base.agents.impersonation.event_log import LOG_PROTOCOL_VERSION
from base.telemetry.alerts import upsert_alert

SEAL_STUCK = "ImpersonationEventSealStuck"
CAPTURE_FAILED = "ImpersonationEventCaptureFailed"
_SOURCE = "health-probe"


def _has_open_participant(conn: psycopg.Connection, lease_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM agent_impersonation_event_participants WHERE lease_id=%s "
        "AND state='open' LIMIT 1",
        (lease_id,),
    ).fetchone()
    return row is not None


def _starts_at(conn: psycopg.Connection, lease: dict[str, Any], alertname: str) -> datetime:
    # One instance per condition episode: the onset comes from stored rows, never the clock.
    if alertname == SEAL_STUCK:
        return cast(datetime, lease["ended_at"])
    row = conn.execute(
        "SELECT min(opened_at) FROM agent_impersonation_event_participants "
        "WHERE lease_id=%s AND state='failed'",
        (lease["id"],),
    ).fetchone()
    failed_at = cast(datetime | None, row[0]) if row is not None else None
    return failed_at or cast(datetime, lease["created_at"])


def _annotations(lease: dict[str, Any]) -> dict[str, str]:
    return {
        "pending_reason": str(lease["event_delivery_pending_reason"] or "unknown"),
        "session": f"{lease['agent_id']}:{lease['session_id']}",
    }


def alert_capture_failed(conn: psycopg.Connection, lease: dict[str, Any]) -> None:
    """Raise the permanent alert for a lease whose source failed to capture."""
    upsert_alert(
        conn,
        {
            "status": "firing",
            "labels": {
                "alertname": CAPTURE_FAILED,
                "severity": "warning",
                "lease_id": str(lease["id"]),
                "machine": str(lease["machine"]),
            },
            "annotations": _annotations(lease),
            "starts_at": _starts_at(conn, lease, CAPTURE_FAILED).isoformat(),
        },
        source=_SOURCE,
    )


def reconcile_seal_stuck_alerts(conn: psycopg.Connection) -> int:
    """Fire for every ended incomplete lease with an open source; resolve cleared ones.

    Returns the number of alerts fired or resolved.
    """
    changed = 0
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM agent_impersonations WHERE automatic "
            "AND event_delivery_protocol_version=%s AND events_completed_at IS NULL "
            "AND ended_at IS NOT NULL",
            (LOG_PROTOCOL_VERSION,),
        )
        ended = cur.fetchall()
        cur.execute(
            "SELECT labels,annotations,starts_at FROM alerts WHERE source=%s "
            "AND status='unresolved' AND alertname=%s",
            (_SOURCE, SEAL_STUCK),
        )
        open_alerts = cur.fetchall()
    stuck = {
        str(lease["id"]): lease for lease in ended if _has_open_participant(conn, str(lease["id"]))
    }
    for lease in stuck.values():
        _key, did_insert, _notify, _row = upsert_alert(
            conn,
            {
                "status": "firing",
                "labels": {
                    "alertname": SEAL_STUCK,
                    "severity": "warning",
                    "lease_id": str(lease["id"]),
                    "machine": str(lease["machine"]),
                },
                "annotations": _annotations(lease),
                "starts_at": _starts_at(conn, lease, SEAL_STUCK).isoformat(),
            },
            source=_SOURCE,
        )
        changed += did_insert
    now = datetime.now(UTC)
    for alert in open_alerts:
        if alert["labels"]["lease_id"] in stuck:
            continue
        upsert_alert(
            conn,
            {
                "status": "resolved",
                "labels": alert["labels"],
                "annotations": alert["annotations"],
                "starts_at": alert["starts_at"].isoformat(),
                "ends_at": now.isoformat(),
            },
            source=_SOURCE,
        )
        changed += 1
    return changed
