"""Lifecycle markers from the audit record: the spawn / restart / terminate events in a window."""

from __future__ import annotations

from datetime import datetime
from typing import cast

from base.db import Database
from base.events import audit_rows
from services.derived.insights.run_timeline.schemas import RunTimelineEvent

# One audit event per user-visible lifecycle step. `resurrect` and
# `restart_completed` are the two ways an agent comes back.
_LIFECYCLE_EVENTS = ["spawn", "resurrect", "restart_completed", "terminate"]
# A transport page, not a display limit: the reader pages until the window is exhausted.
_PAGE_SIZE = 500


def read(db: Database, agent_id: int, start: datetime, end: datetime) -> list[RunTimelineEvent]:
    """The window's lifecycle events, oldest first."""
    rows: list[dict[str, object]] = []
    offset = 0
    with db.connect(autocommit=True) as conn:
        while True:
            page, has_more = audit_rows.query_events(
                conn,
                agent_id=agent_id,
                event_names=_LIFECYCLE_EVENTS,
                from_=start,
                to=end,
                limit=_PAGE_SIZE,
                offset=offset,
                direction="forward",
            )
            rows.extend(page)
            if not has_more:
                break
            offset += _PAGE_SIZE
    events: list[RunTimelineEvent] = []
    for row in rows:
        raw = row["attributes"]
        attrs = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
        label = attrs.get("exc_type") or attrs.get("reason") or attrs.get("body")
        events.append(
            RunTimelineEvent(
                ts=cast(datetime, row["ts"]),
                kind=str(row["event_name"]),
                label=label if isinstance(label, str) else None,
            )
        )
    return events
