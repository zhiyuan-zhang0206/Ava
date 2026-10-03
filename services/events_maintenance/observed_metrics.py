"""Recover persisted metric observations outside the interactive request path.

Every live event is projected into `agent_metric_observations` by the emitter's drain thread; an
event the projection missed (the database did not answer, or the event reached
`telemetry_events` through the mirror replay or a backfill) is still in `telemetry_events`. Each
maintenance pass scans the last seven days of the supported event families, keeps the rows whose
observation is missing, and writes them through the same reduction the live projection uses
(`observe_row`), so a recovered row and a live one are the same observation. The frozen archive
owns the timestamps up to its last row, so rows at or before `ARCHIVE_FREEZE_AT` are left alone.
A scan means the table was traversed, not that upstream collection was lossless. `node_exit`
rows are not stored in `telemetry_events`, so activity observations come from the live
projection only and are not recoverable here.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import Connection

from base.telemetry.loki_index_labels import ARCHIVE_FREEZE_AT
from base.telemetry.metrics.aggregate_sql import EXEC_FAILURE_EVENTS
from base.telemetry.metrics.observed_metrics import (
    MetricObservation,
    observe_row,
    write_observations,
)

_WINDOW = timedelta(days=7)
_PAGE_LIMIT = 5000
_PASS_SECONDS = 120.0
_EVENT_NAMES = ["llm_usage", "turn_end", "exec", *EXEC_FAILURE_EVENTS]

# Event ids in the observation table are the unsigned stream id; the table stores it signed.
_UNSIGNED_UID = (
    "(t.event_uid::numeric + CASE WHEN t.event_uid < 0 THEN 18446744073709551616 ELSE 0 END)"
)

_MISSING = f"""
    SELECT t.event_uid, t.ts, t.agent_id, t.category, t.event_name, t.attributes, {_UNSIGNED_UID}
    FROM telemetry_events t
    WHERE t.ts >= %s AND t.ts < %s AND t.ts > %s AND t.agent_id IS NOT NULL
      AND t.event_name = ANY(%s)
      AND EXISTS (SELECT 1 FROM agents a WHERE a.id = t.agent_id)
      AND NOT EXISTS (SELECT 1 FROM agent_metric_observations o WHERE o.event_id = {_UNSIGNED_UID})
      AND (t.ts, t.event_uid) > (%s, %s)
    ORDER BY t.ts, t.event_uid
    LIMIT %s
"""  # noqa: S608 — the id expression is a constant


def recover_observations(conn: Connection[Any], *, now: datetime | None = None) -> int:
    """One bounded maintenance pass; failures leave already committed pages in place.

    Pages by `(ts, event_uid)` so a row that cannot become an observation is skipped, never
    retried in a loop. Returns how many observations were newly written.
    """
    now = now or datetime.now(UTC)
    deadline = time.monotonic() + _PASS_SECONDS
    cursor_ts, cursor_uid = datetime.min.replace(tzinfo=UTC), -(1 << 63)
    written = 0
    while time.monotonic() < deadline:
        rows = conn.execute(
            _MISSING,  # type: ignore[arg-type]
            (
                now - _WINDOW,
                now,
                ARCHIVE_FREEZE_AT,
                _EVENT_NAMES,
                cursor_ts,
                cursor_uid,
                _PAGE_LIMIT,
            ),
        ).fetchall()
        conn.commit()
        if not rows:
            break
        observations: list[MetricObservation] = []
        for _uid, ts, agent_id, category, event_name, attributes, stream_id in rows:
            try:
                observation = observe_row(
                    {
                        "id": int(stream_id),
                        "ts": ts,
                        "agent_id": agent_id,
                        "category": category,
                        "event_name": event_name,
                        "attributes": attributes,
                    }
                )
            except (TypeError, ValueError, KeyError, ArithmeticError):
                continue
            if observation is not None:
                observations.append(observation)
        cursor_ts, cursor_uid = rows[-1][1], rows[-1][0]
        with conn.transaction():
            written += write_observations(observations, db=conn)
        if len(rows) < _PAGE_LIMIT:
            break
    return written
