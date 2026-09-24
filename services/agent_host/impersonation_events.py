"""Runner-owned replay of late SDK/API facts into permanent session handoffs."""

import asyncio
from datetime import UTC, datetime

from psycopg.rows import dict_row

from ava._impersonation_events import consume_recorded_events, post_completion_integrity_breach
from shared import maintenance
from shared.agents.impersonation_manifest import monitor_manifest_health
from shared.alerts import upsert_alert
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.log import logger
from shared.loki_index_labels import retention_floor
from shared.machine import machine_name


def reconcile_one() -> None:
    """Choose one due local session fairly, independent of agent/model liveness."""
    machine = machine_name()
    monitor_manifest_health(machine=machine)
    _watch_completed_manifest_integrity(machine)
    with write_transaction() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM agent_impersonations WHERE machine=%s AND automatic "
            "AND activated_at IS NOT NULL AND ended_at IS NOT NULL "
            "AND events_completed_at IS NULL AND events_next_read_at<=clock_timestamp() "
            "ORDER BY events_next_read_at,agent_id,session_id LIMIT 1",
            (machine,),
        )
        session = cur.fetchone()
        if session is None:
            return
        # Advancing the due time schedules a retry, not a completeness receipt.
        # Even a failing session rotates behind other pending sessions.
        cur.execute(
            "UPDATE agent_impersonations SET events_next_read_at=clock_timestamp()+%s*interval '1 second' "
            "WHERE id=%s",
            (settings.general.impersonation_event_reconcile_interval_seconds, session["id"]),
        )
    consume_recorded_events(session, page_budget=settings.general.impersonation_event_page_budget)


def _watch_completed_manifest_integrity(machine: str) -> None:
    """Probe completed local manifests only while their full Loki range exists."""
    with write_transaction() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM agent_impersonations WHERE machine=%s AND automatic "
            "AND event_delivery_protocol_version=1 AND events_completed_at IS NOT NULL "
            "AND event_delivery_integrity_alerted_at IS NULL "
            "AND manifest_envelope_floor_at >= %s ORDER BY events_completed_at",
            (machine, retention_floor()),
        )
        leases = cur.fetchall()
    for lease in leases:
        if post_completion_integrity_breach(lease):
            _record_integrity_alert(lease)


def _record_integrity_alert(lease: dict[str, object]) -> None:
    with write_transaction() as conn:
        stamped = conn.execute(
            "SELECT record_impersonation_event_integrity_alert(%s)", (lease["id"],)
        ).fetchone()
        if stamped is None or stamped[0] is not True:
            return
        upsert_alert(
            conn,
            {
                "status": "firing",
                "labels": {
                    "alertname": "ImpersonationEventDeliveryIntegrity",
                    "severity": "error",
                    "lease_id": str(lease["id"]),
                    "machine": str(lease["machine"]),
                },
                "annotations": {"session": f"{lease['agent_id']}:{lease['session_id']}"},
                "starts_at": datetime.now(UTC).isoformat(),
            },
            source="machine-probe",
        )


async def reconcile_forever() -> None:
    """Replay at the runner's maintenance cadence without blocking ownership renewal."""
    while True:
        if not maintenance.quiesced():
            try:
                await asyncio.to_thread(reconcile_one)
            except Exception:
                logger.exception("Impersonation event replay failed; pending session will retry")
        await asyncio.sleep(settings.general.impersonation_event_reconcile_interval_seconds)
