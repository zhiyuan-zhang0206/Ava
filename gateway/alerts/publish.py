"""Publish alert rows to the live SSE channel (best-effort).

Split out of the router so the processes that raise or repair alerts without
serving HTTP (the events-maintenance service's Grafana reconciliation) publish
through the same code as the ingest endpoint.
"""

from __future__ import annotations

import logging
from typing import Any

from base.events.live.bus import EventBus
from gateway.alerts.schemas import AlertRow

_log = logging.getLogger(__name__)

# The Redis pub/sub channel every ingest publishes to and the SSE stream
# subscribes to.
ALERTS_CHANNEL = "ava:alerts"


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
