"""Warning/error classes of the stats window, one row per class — the sidebar card's detail.

The dashboard card counts active classes instead of raw events, the way an error tracker
groups an event stream: `GET /api/stats/alert-classes` lists each
`(level, event_name, source, process)` class of the selected window with its count, first and
last occurrence and the dismissal that cancels it, and `.../samples` opens one class to its
newest events. Dismissing and reopening go through `/api/event-resolutions`
(`gateway/events/resolutions.py`); the dismissal match is the events-maintenance daemon's
(`services.events_maintenance.resolution.matching_dismissal`), so a class reads as dismissed
here exactly when the unresolved gauges stop counting it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, cast

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.rows import dict_row

from base.telemetry.observability import cluster_label
from gateway.cluster.schemas import (
    AlertClassesResponse,
    AlertClassRow,
    AlertClassSample,
    AlertClassSamples,
    AlertLevel,
)
from gateway.schemas.stats import StatsWindowHours, window_delta
from services.events_maintenance import resolution

router = APIRouter()

ALERT_CLASSES_LIMIT = 200
SAMPLES_LIMIT = 5


def read_alert_classes(
    conn: psycopg.Connection[Any], *, start: datetime, end: datetime
) -> list[AlertClassRow]:
    """Every warning/error class over `(start, end]` for the home cluster, most frequent first."""
    active = resolution.active_dismissals(conn)
    rows: list[AlertClassRow] = []
    for found in resolution.alert_classes(conn, start=start, end=end, cluster=cluster_label()):
        dismissal = resolution.matching_dismissal(
            resolution.EventClass(
                category=found.category,
                level=found.level,
                event_name=found.event_name,
                source=found.source,
                process=found.process,
            ),
            active,
        )
        rows.append(
            AlertClassRow(
                level=cast(AlertLevel, found.level),
                event_name=found.event_name,
                source=found.source,
                process=found.process,
                category=cast(Any, found.category),
                count=found.count,
                first_seen=found.first_seen,
                last_seen=found.last_seen,
                dismissal_id=dismissal.id if dismissal is not None else None,
            )
        )
    return rows


@router.get("/api/stats/alert-classes")
def get_alert_classes(
    request: Request,
    hours: Annotated[StatsWindowHours, Query()] = StatsWindowHours.H24,
) -> AlertClassesResponse:
    """The selected window's warning/error classes, most frequent first.

    Active and dismissed classes come together (`dismissal_id` tells them apart) so the console
    can list the dismissed ones apart and reopen them. Capped at `ALERT_CLASSES_LIMIT` rows;
    `total_classes` / `total_events` are uncapped. One connection, an 8-second statement
    timeout (a timeout is a retriable 503).
    """
    now = datetime.now(UTC)
    try:
        with request.app.state.db_pool.connection() as conn:
            conn.execute("SET LOCAL statement_timeout = '8s'")
            classes = read_alert_classes(conn, start=now - window_delta(hours), end=now)
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="alert classes read timed out; retry") from exc
    return AlertClassesResponse(
        window_hours=hours,
        classes=classes[:ALERT_CLASSES_LIMIT],
        total_classes=len(classes),
        total_events=sum(row.count for row in classes),
        as_of=datetime.now(UTC),
    )


@router.get("/api/stats/alert-classes/samples")
def get_alert_class_samples(
    request: Request,
    level: Annotated[AlertLevel, Query()],
    event_name: Annotated[str, Query(min_length=1, max_length=255)],
    source: Annotated[str, Query(min_length=1, max_length=255)],
    process: Annotated[str, Query(max_length=255)] = "",
    hours: Annotated[StatsWindowHours, Query()] = StatsWindowHours.H24,
) -> AlertClassSamples:
    """The newest `SAMPLES_LIMIT` events of one class in the window, with their message and context.

    `process` matches exactly (an empty value is the empty-process class). The literal level
    predicate lets the partial index on warning/error/critical rows serve the newest-first scan.
    """
    now = datetime.now(UTC)
    try:
        with (
            request.app.state.db_pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            cur.execute("SET LOCAL statement_timeout = '8s'")
            cur.execute(
                """
                SELECT ts, agent_id, machine, trace_id, attributes->>'msg' AS message, attributes
                FROM telemetry_events
                WHERE level IN ('warning', 'error', 'critical')
                  AND level = %s AND event_name = %s AND source = %s AND process = %s
                  AND (cluster = %s OR cluster = '')
                  AND ts > %s AND ts <= %s
                ORDER BY ts DESC
                LIMIT %s
                """,
                (
                    level,
                    event_name,
                    source,
                    process,
                    cluster_label(),
                    now - window_delta(hours),
                    now,
                    SAMPLES_LIMIT,
                ),
            )
            rows = cur.fetchall()
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="alert samples read timed out; retry") from exc
    return AlertClassSamples(samples=[AlertClassSample(**row) for row in rows])
