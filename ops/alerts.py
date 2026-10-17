"""The alert row and its live SSE channel (best-effort publish).

Shared by the gateway's alert endpoints and the processes that raise or repair
alerts without serving HTTP (the events-maintenance service's Grafana
reconciliation), so both publish the same validated row shape through the same code.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from base.events.live.bus import EventBus

_log = logging.getLogger(__name__)

# Store vocabulary (user design 2026-08-12): three severities, two statuses.
AlertSeverity = Literal["critical", "warning", "error"]
AlertStatus = Literal["unresolved", "resolved"]


class AlertRow(BaseModel):
    """One alerts row as the UI sees it (list + SSE frames)."""

    model_config = ConfigDict(frozen=True)

    id: int
    status: AlertStatus
    severity: AlertSeverity
    alertname: str
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: datetime
    ends_at: datetime | None
    fingerprint: str
    generator_url: str
    source: str
    notified_at: datetime | None
    created_at: datetime
    updated_at: datetime


# The Redis pub/sub channel every ingest publishes to and the SSE stream
# subscribes to.
ALERTS_CHANNEL = "meiva:alerts"


def publish_alert_rows(bus: EventBus, rows: list[dict[str, Any]]) -> None:
    """Publish each upserted row to the SSE channel (best-effort).

    A Redis outage must not fail the ingest — the SSE stream is a live tail
    and the UI's initial fetch carries the same rows."""

    if not rows:
        return
    try:
        with bus.sync_redis() as client:
            for row in rows:
                frame = AlertRow(**row).model_dump_json()
                client.publish(ALERTS_CHANNEL, frame)  # pyright: ignore[reportUnknownMemberType] — redis-py from_url kwargs typed Unknown (same pattern as base/events/live/redis_client.py)
    except Exception:
        _log.warning("alerts: SSE publish failed (Redis unreachable?)", exc_info=True)
