"""Stored-onset identity and producer resolution for manifest health alerts."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

from base.agents.impersonation.event_log import LOG_PROTOCOL_VERSION
from base.config import settings
from base.telemetry.loki_index_labels import EVENT_STREAM_RETENTION

_MANIFEST_ALERT_NAMES = (
    "ImpersonationManifestSealSlow",
    "ImpersonationEventRetentionLoss",
    "ImpersonationEventDeliveryPending",
    "ImpersonationManifestCaptureFailed",
    "ImpersonationEventSealStuck",
)


def _retention_lost(lease: dict[str, Any], *, horizon: datetime) -> bool:
    return (
        lease["manifest_frozen_at"] is not None
        and lease["manifest_envelope_floor_at"] < horizon
        and lease["events_completed_at"] is None
    )


def _has_slow_open_participant(conn: psycopg.Connection, lease_id: str, *, now: datetime) -> bool:
    row = conn.execute(
        "SELECT 1 FROM agent_impersonation_event_participants WHERE lease_id=%s "
        "AND state='open' AND opened_at<%s LIMIT 1",
        (
            lease_id,
            now
            - timedelta(seconds=settings.general.impersonation_event_manifest_seal_wait_seconds),
        ),
    ).fetchone()
    return row is not None


def _seal_stuck_leases(conn: psycopg.Connection, machine: str) -> list[dict[str, Any]]:
    """Ended log-native leases still waiting on an open participant (a state, no threshold)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM agent_impersonations WHERE machine=%s AND automatic "
            "AND event_delivery_protocol_version=%s AND events_completed_at IS NULL "
            "AND ended_at IS NOT NULL",
            (machine, LOG_PROTOCOL_VERSION),
        )
        leases = cur.fetchall()
    return [lease for lease in leases if _has_open_participant(conn, str(lease["id"]))]


def _has_open_participant(conn: psycopg.Connection, lease_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM agent_impersonation_event_participants WHERE lease_id=%s "
        "AND state='open' LIMIT 1",
        (lease_id,),
    ).fetchone()
    return row is not None


def _is_old_pending(lease: dict[str, Any], *, now: datetime) -> bool:
    ended = lease["ended_at"]
    return ended is not None and ended < now - timedelta(
        seconds=settings.general.impersonation_event_delivery_alert_age_seconds
    )


def _manifest_alert_starts_at(
    conn: psycopg.Connection, lease: dict[str, Any], alertname: str
) -> datetime:
    # One instance per condition episode: derive starts_at from stored onset, never wall-clock now.
    if alertname == "ImpersonationManifestSealSlow":
        row = conn.execute(
            "SELECT min(opened_at) FROM agent_impersonation_event_participants "
            "WHERE lease_id=%s AND state='open'",
            (lease["id"],),
        ).fetchone()
        opened_at = cast(datetime | None, row[0]) if row is not None else None
        if opened_at is None:
            raise RuntimeError("Slow manifest alert requires an open participant")
        return opened_at + timedelta(
            seconds=settings.general.impersonation_event_manifest_seal_wait_seconds
        )
    if alertname == "ImpersonationEventSealStuck":
        # A log-native lease that ended with a source still open: the state is
        # the condition, so the onset is the end of the lease and no threshold applies.
        return cast(datetime, lease["ended_at"])
    if alertname == "ImpersonationEventRetentionLoss":
        return lease["manifest_envelope_floor_at"] + EVENT_STREAM_RETENTION
    if alertname == "ImpersonationEventDeliveryPending":
        return lease["ended_at"] + timedelta(
            seconds=settings.general.impersonation_event_delivery_alert_age_seconds
        )
    if alertname == "ImpersonationManifestCaptureFailed":
        row = conn.execute(
            "SELECT min(opened_at) FROM agent_impersonation_event_participants "
            "WHERE lease_id=%s AND state='failed'",
            (lease["id"],),
        ).fetchone()
        failed_at = cast(datetime | None, row[0]) if row is not None else None
        return failed_at or cast(datetime, lease["created_at"])
    raise ValueError(f"Unknown manifest alert {alertname!r}")


def _resolve_cleared_manifest_alerts(
    conn: psycopg.Connection,
    upsert_alert: Any,
    machine: str,
    now: datetime,
    horizon: datetime,
) -> None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT alertname,labels,annotations,starts_at FROM alerts "
            "WHERE source='machine-probe' AND status='unresolved' "
            "AND labels->>'machine'=%s AND alertname=ANY(%s)",
            (machine, list(_MANIFEST_ALERT_NAMES)),
        )
        alerts = cur.fetchall()
    for alert in alerts:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM agent_impersonations WHERE id=%s",
                (alert["labels"]["lease_id"],),
            )
            lease = cur.fetchone()
        condition_holds = False
        if lease is not None and lease["events_completed_at"] is None:
            alertname = alert["alertname"]
            if alertname == "ImpersonationEventRetentionLoss":
                condition_holds = _retention_lost(lease, horizon=horizon)
            elif alertname == "ImpersonationManifestSealSlow":
                condition_holds = _has_slow_open_participant(conn, str(lease["id"]), now=now)
            elif alertname == "ImpersonationEventDeliveryPending":
                condition_holds = _is_old_pending(lease, now=now)
            elif alertname == "ImpersonationEventSealStuck":
                condition_holds = lease["ended_at"] is not None and _has_open_participant(
                    conn, str(lease["id"])
                )
            elif alertname == "ImpersonationManifestCaptureFailed":
                condition_holds = (
                    conn.execute(
                        "SELECT 1 FROM agent_impersonation_event_participants "
                        "WHERE lease_id=%s AND state='failed' LIMIT 1",
                        (lease["id"],),
                    ).fetchone()
                    is not None
                )
            else:
                raise ValueError(f"Unknown manifest alert {alertname!r}")
        if (
            condition_holds
            and lease is not None
            and alert["starts_at"] == _manifest_alert_starts_at(conn, lease, alert["alertname"])
        ):
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
            source="machine-probe",
        )
