"""Computer-use trace endpoints — /api/computer/traces.

One task's desktop-action trail, assembled from the unified events stream:
computer_session_start / computer_action / computer_session_end rows whose
`attributes.task_id` matches (Phase 2, task #1101). The replay page and any
audit consumer read one endpoint instead of hand-joining three event names.

Reads Loki (task #1197): the event stream lives in the LGTM stack, and a
trace is always a small bounded read (500 rows) — the exact SQL shape the
PG path used, minus the table.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from gateway import loki_events

router = APIRouter()

_TRACE_EVENT_NAMES = (
    "computer_action",
    "computer_session_start",
    "computer_session_end",
)

# A trace is bounded: 500 rows covers long sessions without unbounded scans.
_TRACE_LIMIT = 500


@router.get("/api/computer/traces")
def get_computer_trace(
    task_id: int = Query(..., ge=1, description="task id whose desktop trail to read"),
) -> dict[str, Any]:
    """The desktop-action trail for one task: session envelope + actions.

    Returns:
      {task_id, actions: [{id, ts, agent_id, event, action, app, outcome,
        coords, path, error}...], start: {...}|null, end: {...}|null}
    — chronological, one entry per event row. `start`/`end` are the session
    envelope rows (may be null while a session is open); `actions` carries
    the computer_action rows with the replay-relevant payload keys.
    """
    rows, _has_more = loki_events.query_events(
        attribute_filters={"task_id": str(task_id)},
        event_names=list(_TRACE_EVENT_NAMES),
        limit=_TRACE_LIMIT,
        direction="forward",
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"no computer-use trace for task {task_id}")
    actions: list[dict[str, Any]] = []
    start: dict[str, Any] | None = None
    end: dict[str, Any] | None = None
    for row in rows:
        attrs: dict[str, Any] = row.get("attributes") or {}
        ts = row.get("ts")
        entry: dict[str, Any] = {
            "id": row["id"],
            "ts": ts.isoformat() if ts is not None else None,
            "agent_id": row.get("agent_id"),
            "event": row.get("event_name"),
        }
        if row.get("event_name") == "computer_action":
            entry.update(
                {
                    "action": attrs.get("action"),
                    "app": attrs.get("app"),
                    "outcome": attrs.get("outcome"),
                    "coords": attrs.get("coords"),
                    "path": attrs.get("path"),
                    "error": attrs.get("error"),
                }
            )
            actions.append(entry)
        elif row.get("event_name") == "computer_session_start":
            start = entry | {
                "first_tool": attrs.get("first_tool"),
                "first_action_at": attrs.get("first_action_at"),
            }
        else:  # computer_session_end
            end = entry | {
                "action_count": attrs.get("action_count"),
                "first_action_at": attrs.get("first_action_at"),
                "last_action_at": attrs.get("last_action_at"),
                "outcome": attrs.get("outcome"),
            }
    return {"task_id": task_id, "start": start, "end": end, "actions": actions}
