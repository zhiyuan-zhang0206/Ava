"""Task registry endpoints — read the full agent_tasks table and update tasks.

GET /api/tasks — the full task list (the registry).
PATCH /api/tasks/{id} — partial update (status / title / description /
results / remind_interval_seconds / owner).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from gateway.schemas import TaskListResponse, TaskRow, TaskUpdateRequest
from shared.task_reparent import resolve_reparent

router = APIRouter()

# Reminders cannot be disabled, so remind_interval_seconds is capped at 24h and must be
# positive — mirrors ava_builtins/plugins/ava_fleet/task_registry._MAX_REMIND_INTERVAL_SECONDS
# across the SDK/gateway layer boundary (gateway may not import plugins).
_MAX_REMIND_INTERVAL_SECONDS = 86400

_TASK_COLS = (
    "t.id, t.parent_id, t.title, t.description, t.results, t.status, t.owner, "
    "t.created_by, t.created_at, t.updated_at, "
    "t.remind_interval_seconds, t.last_reminded_at, t.reminder_count, t.priority"
)


def _row_to_task(row: tuple[Any, ...], owner_label: str | None = None) -> TaskRow:
    """Build a TaskRow from a row selected in _TASK_COLS order, plus an
    optional owner_label from a LEFT JOIN with the agents table."""
    return TaskRow(
        id=row[0],
        parent_id=row[1],
        title=row[2],
        description=row[3],
        results=row[4],
        status=row[5],
        owner=row[6],
        owner_label=owner_label,
        created_by=row[7],
        created_at=row[8].isoformat(),
        updated_at=row[9].isoformat(),
        remind_interval_seconds=row[10],
        last_reminded_at=row[11].isoformat() if row[11] else None,
        reminder_count=row[12],
        priority=row[13],
    )


@router.get("/api/tasks")
def get_tasks(request: Request) -> TaskListResponse:
    """Return every task in the agent_tasks table, newest first.

    No pagination — the task registry is bounded (tasks are created by agents;
    closed tasks are kept). Order by created_at DESC so the newest tasks
    surface first in the Task Graph view.
    """
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_TASK_COLS}, a.label AS owner_label "  # noqa: S608
            "FROM agent_tasks t "
            "LEFT JOIN agents a ON t.owner = a.id "
            "ORDER BY t.created_at DESC"
        )
        tasks = [_row_to_task(r[:-1], owner_label=r[-1]) for r in cur.fetchall()]
    return TaskListResponse(tasks=tasks)


def _collect_updates(body: TaskUpdateRequest) -> tuple[list[str], list[object]]:
    """Validate the PATCH body and collect its SET clauses + params;
    raises HTTPException 422 on an invalid field."""
    sets: list[str] = []
    params: list[object] = []
    if body.status is not None:
        if body.status not in ("open", "in_progress", "done", "cancelled"):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status: {body.status!r}. Must be one of: open, in_progress, done, cancelled.",
            )
        sets.append("status = %s")
        params.append(body.status)
    # priority is typed as the shared Priority enum, so pydantic already 422s
    # any value outside P0..P3 before this runs.
    if body.priority is not None:
        sets.append("priority = %s")
        params.append(body.priority.value)
    if body.title is not None:
        sets.append("title = %s")
        params.append(body.title)
    if body.description is not None:
        sets.append("description = %s")
        params.append(body.description)
    if body.results is not None:
        sets.append("results = %s")
        params.append(body.results)
    if "owner" in body.model_fields_set:
        if body.owner is None:
            raise HTTPException(
                status_code=422,
                detail="owner cannot be null: a task cannot be released. "
                "Reassign to an agent id instead.",
            )
        sets.append("owner = %s")
        params.append(body.owner)
    if "remind_interval_seconds" in body.model_fields_set:
        if body.remind_interval_seconds is None:
            raise HTTPException(
                status_code=422,
                detail="remind_interval_seconds cannot be null: reminders cannot be disabled.",
            )
        if not 0 < body.remind_interval_seconds <= _MAX_REMIND_INTERVAL_SECONDS:
            raise HTTPException(
                status_code=422,
                detail=f"remind_interval_seconds must be a positive number of seconds <= "
                f"{_MAX_REMIND_INTERVAL_SECONDS} (24h), got {body.remind_interval_seconds!r}.",
            )
        sets.append("remind_interval_seconds = %s")
        params.append(body.remind_interval_seconds)
    return sets, params


@router.patch("/api/tasks/{task_id}")
def patch_task(task_id: int, body: TaskUpdateRequest, request: Request) -> TaskRow:
    """Partially update a task; omitted fields stay unchanged.

    status, priority, title, description, and results are taken when non-null
    (priority must be one of P0..P3; a title colliding with another
    open/in_progress task's is rejected). owner reassigns to another agent (an
    explicit null is rejected — a task cannot be released).
    remind_interval_seconds must be a positive number of seconds <= 24h (an explicit
    null is rejected — reminders cannot be disabled). Any write resets the
    reminder counters, same as the SDK update path. Unlike the SDK, an owner change here
    does NOT message the affected agents — this endpoint is a plain column write.

    The system root task is immutable: any PATCH targeting it is rejected with
    422 (mirrors the SDK update() guard), so the task-tree anchor can never be
    reassigned, completed, cancelled, or otherwise edited.
    """
    sets, params = _collect_updates(body)
    if "parent_id" in body.model_fields_set:
        # Reparenting needs a cursor (root-id resolution + cycle/existence
        # checks), so its SET clause is built inside the transaction below.
        sets.append("parent_id = %s")
        params.append(body.parent_id)
    if not sets:
        raise HTTPException(
            status_code=422,
            detail="Nothing to update: pass at least one of status, priority, "
            "title, description, results, remind_interval_seconds, owner, parent_id.",
        )
    # Reset the reminder counters on any update — a fresh overdue window starts
    # from this update, so any previous reminder is stale (same rule as the SDK).
    sets.append("last_reminded_at = NULL")
    sets.append("reminder_count = 0")

    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        # The system root task is immutable (the task-tree anchor / default
        # parent) — reject any edit before writing, same rule as the SDK
        # update() path. Check existence here too so a missing task still 404s.
        cur.execute("SELECT is_root FROM agent_tasks WHERE id = %s", (task_id,))
        guard = cur.fetchone()
        if guard is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found.")
        if guard[0]:
            raise HTTPException(
                status_code=422,
                detail=f"Task {task_id} is the system root task and is immutable.",
            )
        # Reparenting mirrors the SDK update() checks (shared validation):
        # an explicit null moves the task under the system root; the parent
        # must exist, must not be the task itself, not a descendant.
        if "parent_id" in body.model_fields_set:
            try:
                # The placeholder value is appended last (above the
                # reminder-reset SETs which carry no params); replace it with
                # the resolved root id / validated parent before the UPDATE.
                params[-1] = resolve_reparent(cur, task_id, body.parent_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        # A rename must keep the SDK create() invariant: no two open/in_progress
        # tasks share a title.
        if body.title is not None:
            cur.execute(
                "SELECT id, status FROM agent_tasks "
                "WHERE title = %s AND status IN ('open', 'in_progress') AND id != %s LIMIT 1",
                (body.title, task_id),
            )
            dup = cur.fetchone()
            if dup is not None:
                raise HTTPException(
                    status_code=422,
                    detail=f"A task with title {body.title!r} already exists "
                    f"(task {dup[0]} is {dup[1]}); duplicate open/in_progress "
                    f"titles are not allowed.",
                )
        cur.execute(
            f"WITH updated AS ( "  # noqa: S608
            f"    UPDATE agent_tasks AS t SET {', '.join(sets)}, updated_at = now() "
            f"    WHERE t.id = %s "
            f"    RETURNING {_TASK_COLS} "
            f") "
            f"SELECT u.*, a.label AS owner_label "
            f"FROM updated u "
            f"LEFT JOIN agents a ON u.owner = a.id",
            (*params, task_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found.")

    return _row_to_task(row[:-1], owner_label=row[-1])
