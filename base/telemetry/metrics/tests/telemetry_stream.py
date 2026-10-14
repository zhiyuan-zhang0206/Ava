"""Writes telemetry events into the real `telemetry_events` table for the metrics read tests.

`telemetry_events` is append-only, so a test cannot clear what earlier tests wrote. Each
`TelemetryStream` therefore owns its own stretch of time: its `now` is a distinct instant (a
random slot, spaced wider than the longest metrics window), the rows it writes sit at offsets
before that instant, and the test hands `now` to the read under test, so the rows of other tests
never fall into its window.
"""

from __future__ import annotations

import json
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

# Wider than the longest metrics window (30 days), so two streams' windows never overlap.
_SPACING_DAYS = 40


class TelemetryStream:
    def __init__(self, db: psycopg.Connection) -> None:
        db.autocommit = True
        self.db = db
        slot = random.SystemRandom().randrange(1, 2000)
        self.now = datetime(2000, 1, 1, 12, tzinfo=UTC) + timedelta(days=_SPACING_DAYS * slot)

    def add(
        self,
        *,
        event: str,
        agent_id: int | None,
        payload: dict[str, Any] | None = None,
        ts_offset_hours: float = 0,
    ) -> None:
        self.db.execute(
            "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
            "category, event_name, level, source, attributes) VALUES (%s, %s, %s, 'm', 'c', 'p', "
            "%s, %s, 'info', 'test', %s::jsonb)",
            (
                uuid.uuid4().int % (1 << 62),
                self.now - timedelta(hours=ts_offset_hours),
                agent_id,
                "log" if event == "log" else "telemetry",
                event,
                json.dumps(payload or {}),
            ),
        )
