"""Authenticated class-level warning/error resolution API (task #1468).

Loki event lines are immutable. This router records a state transition in
``event_dismissals`` and emits a matching telemetry marker; the events-
maintenance daemon later combines that state with its fixed-window Loki count
to publish the unresolved gauges. There is deliberately no PG event write-back.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from psycopg.rows import dict_row

from gateway.schemas.event_resolutions import (
    EventResolutionCreate,
    EventResolutionListResponse,
    EventResolutionRow,
    EventResolutionStatus,
)
from shared import telemetry
from shared.db_transaction import write_transaction

router = APIRouter()


def _resolution_attributes(row: dict[str, Any]) -> dict[str, object]:
    """The immutable class attributes shared by resolved/reopened markers."""

    return {
        "category": row["category"],
        "level": row["level"],
        "event_name": row["event_name"],
        "source": row["source"],
        "agent_id": row["agent_id"],
        "dismissed_by": row["dismissed_by"],
        "note": row["note"],
    }


def _marker_name(row: dict[str, Any], action: str) -> str:
    """Critical target classes share the error marker family with errors."""

    prefix = "warning" if row["level"] == "warning" else "error"
    return f"{prefix}_{action}"


@router.post(
    "/api/event-resolutions",
    response_model=EventResolutionRow,
    status_code=status.HTTP_201_CREATED,
)
def create_event_resolution(body: EventResolutionCreate, request: Request) -> EventResolutionRow:
    """Dismiss one event class and emit its immutable transition marker.

    Gateway auth establishes that this is an operator action but does not carry
    an agent identity, so `dismissed_by=0` is the documented user/operator
    sentinel. The ops agent uses the same authenticated API surface.
    """

    with (
        write_transaction(request.app.state.db_pool) as conn,
        conn.cursor(row_factory=dict_row) as cur,
    ):
        cur.execute(
            """
            INSERT INTO event_dismissals
                (category, level, event_name, source, agent_id, dismissed_by, note)
            VALUES (%s, %s, %s, %s, %s, 0, %s)
            ON CONFLICT (category, level, event_name, source, agent_id)
                WHERE status = 'dismissed' DO NOTHING
            RETURNING id, category, level, event_name, source, agent_id, dismissed_by, note,
                      status, dismissed_at, reopened_at, burst_count, created_at, updated_at
            """,
            (
                body.category,
                body.level,
                body.event_name,
                body.source,
                body.agent_id,
                body.note,
            ),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail="event class is already dismissed")
        conn.commit()

    telemetry.emit(
        "telemetry",
        _marker_name(row, "resolved"),
        source="gateway",
        attributes=_resolution_attributes(row),
    )
    return EventResolutionRow(**row)


@router.get("/api/event-resolutions", response_model=EventResolutionListResponse)
def list_event_resolutions(
    request: Request,
    status: Annotated[EventResolutionStatus | None, Query()] = None,
) -> EventResolutionListResponse:
    """List active dismissals or reopened history for the ops review cycle."""

    with request.app.state.db_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if status is None:
            cur.execute(
                """
                SELECT id, category, level, event_name, source, agent_id, dismissed_by, note,
                       status, dismissed_at, reopened_at, burst_count, created_at, updated_at
                FROM event_dismissals
                ORDER BY dismissed_at DESC, id DESC
                """
            )
        else:
            cur.execute(
                """
                SELECT id, category, level, event_name, source, agent_id, dismissed_by, note,
                       status, dismissed_at, reopened_at, burst_count, created_at, updated_at
                FROM event_dismissals
                WHERE status = %s
                ORDER BY dismissed_at DESC, id DESC
                """,
                (status,),
            )
        rows = cur.fetchall()
    return EventResolutionListResponse(resolutions=[EventResolutionRow(**row) for row in rows])


@router.post(
    "/api/event-resolutions/{dismissal_id}/reopen",
    response_model=EventResolutionRow,
)
def reopen_event_resolution(dismissal_id: int, request: Request) -> EventResolutionRow:
    """Manually reopen one active dismissal and emit its transition marker."""

    with (
        write_transaction(request.app.state.db_pool) as conn,
        conn.cursor(row_factory=dict_row) as cur,
    ):
        cur.execute(
            """
            UPDATE event_dismissals
            SET status = 'reopened', reopened_at = now(), burst_count = NULL, updated_at = now()
            WHERE id = %s AND status = 'dismissed'
            RETURNING id, category, level, event_name, source, agent_id, dismissed_by, note,
                      status, dismissed_at, reopened_at, burst_count, created_at, updated_at
            """,
            (dismissal_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="active event dismissal not found")
        conn.commit()

    attributes = _resolution_attributes(row)
    attributes.update({"reopened_by": "user/operator", "triggered_by_count": None})
    telemetry.emit(
        "telemetry",
        _marker_name(row, "reopened"),
        source="gateway",
        attributes=attributes,
    )
    return EventResolutionRow(**row)
