"""Task registry endpoints — read the full agent_tasks table and update tasks.

GET /api/tasks — the task list (optionally narrowed by a last-activity window).
PATCH /api/tasks/{id} — partial update (status / title / description /
results / remind_interval_seconds / owner). Closing a parent is rejected while
any child remains active.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, LiteralString, cast, overload

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from psycopg_pool import ConnectionPool

from gateway.routers._eval_guard import deny_isolated_result_read
from gateway.routers.agents_state import post_agent_system_note
from gateway.schemas import (
    SystemNoteIn,
    TaskListResponse,
    TaskRow,
    TaskSummaryRow,
    TaskUpdateRequest,
)
from shared.db_transaction import write_transaction
from shared.task_owner_notifications import TaskOwnerNotification, owner_change_notifications
from shared.task_reparent import resolve_reparent

router = APIRouter()

# Reminders cannot be disabled, so remind_interval_seconds is capped at 24h and must be
# positive — mirrors ava_builtins/plugins/ava_fleet/task_registry._MAX_REMIND_INTERVAL_SECONDS
# across the SDK/gateway layer boundary (gateway may not import plugins).
_MAX_REMIND_INTERVAL_SECONDS = 86400

# Last-activity windows for GET /api/tasks (the task graph's time filter).
# "all" disables the filter entirely.
_TASK_WINDOWS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

_TASK_COLS = (
    "t.id, t.parent_id, t.title, t.description, t.results, t.status, t.owner, "
    "t.created_by, t.created_at, t.updated_at, "
    "t.remind_interval_seconds, t.last_reminded_at, t.reminder_count, t.priority, "
    "t.token_budget, t.usd_budget, t.token_used, t.usd_used"
)

_TASK_SUMMARY_COLS = (
    "t.id, t.parent_id, t.title, t.status, t.owner, t.created_by, t.created_at, t.updated_at, "
    "t.remind_interval_seconds, t.last_reminded_at, t.reminder_count, t.priority, "
    "t.token_budget, t.usd_budget, t.token_used, t.usd_used"
)


@overload
def _row_to_task(
    row: tuple[Any, ...],
    fields: Literal["full"],
    owner_label: str | None = None,
) -> TaskRow: ...


@overload
def _row_to_task(
    row: tuple[Any, ...],
    fields: Literal["summary"],
    owner_label: str | None = None,
) -> TaskSummaryRow: ...


def _row_to_task(
    row: tuple[Any, ...],
    fields: Literal["full", "summary"],
    owner_label: str | None = None,
) -> TaskRow | TaskSummaryRow:
    """Build a task row from the selected projection plus its owner label."""
    if fields == "summary":
        return TaskSummaryRow(
            id=row[0],
            parent_id=row[1],
            title=row[2],
            status=row[3],
            owner=row[4],
            owner_label=owner_label,
            created_by=row[5],
            created_at=row[6].isoformat(),
            updated_at=row[7].isoformat(),
            remind_interval_seconds=row[8],
            last_reminded_at=row[9].isoformat() if row[9] else None,
            reminder_count=row[10],
            priority=row[11],
            token_budget=row[12],
            usd_budget=row[13],
            token_used=row[14],
            usd_used=row[15],
        )
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
        token_budget=row[14],
        usd_budget=row[15],
        token_used=row[16],
        usd_used=row[17],
    )


def _windowed_tasks(
    tasks: list[TaskRow | TaskSummaryRow], window: str
) -> list[TaskRow | TaskSummaryRow]:
    """Narrow the registry to a last-activity window, keeping tree structure.

    Kept tasks: the system root (parent_id NULL), every not-finished task
    (in_progress / ongoing) regardless of age, and every task whose updated_at
    falls inside the window. Each kept task's ancestors that fall outside the
    window are delivered too, flagged ghost=True — the graph renders them
    dimmed so a kept task never dangles as a fake orphan (the client renders
    toggle-hidden parents the same way). done/cancelled tasks outside the
    window with no kept descendant are omitted. Order preserved.
    """
    cutoff = datetime.now(UTC) - _TASK_WINDOWS[window]
    kept: set[int] = set()
    for t in tasks:
        if (
            t.parent_id is None
            or t.status in ("in_progress", "ongoing")
            or datetime.fromisoformat(t.updated_at) >= cutoff
        ):
            kept.add(t.id)

    by_id = {t.id: t for t in tasks}
    ghost: set[int] = set()
    for tid in list(kept):
        pid = by_id[tid].parent_id
        # Walk up while the parent exists, is outside the kept set, and has
        # not been collected yet. A corrupt parent cycle terminates because
        # each visited id lands in `ghost` before its children are followed.
        while pid is not None and pid not in kept and pid not in ghost:
            ghost.add(pid)
            parent = by_id.get(pid)
            pid = parent.parent_id if parent is not None else None

    result: list[TaskRow | TaskSummaryRow] = []
    for t in tasks:
        if t.id in kept:
            result.append(t)
        elif t.id in ghost:
            result.append(t.model_copy(update={"ghost": True}))
    return result


@router.get("/api/tasks", dependencies=[Depends(deny_isolated_result_read)])
def get_tasks(
    request: Request,
    window: str = Query("all", pattern="^(24h|7d|30d|all)$"),
    fields: Literal["full", "summary"] = Query("full"),
) -> TaskListResponse:
    """Return the task registry, newest first.

    `fields=full` (the default) returns the compatibility projection. `fields=summary`
    returns metadata only, selecting neither task text column. `window` (24h / 7d / 30d / all, default all) narrows the list by last activity
    (updated_at) on the backend, so the task graph's default 24-hour view never
    pulls the full registry. A windowed list still carries every kept task's
    out-of-window ancestors flagged ghost=True (see _windowed_tasks); without
    a window the full table is returned unchanged.

    No pagination — the task registry is bounded (tasks are created by agents;
    closed tasks are kept). Order by created_at DESC so the newest tasks
    surface first in the Task Graph view.
    """
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_TASK_COLS if fields == 'full' else _TASK_SUMMARY_COLS}, a.label AS owner_label "  # noqa: S608
            "FROM agent_tasks t "
            "LEFT JOIN agents a ON t.owner = a.id "
            "ORDER BY t.created_at DESC"
        )
        tasks = [_row_to_task(r[:-1], fields, owner_label=r[-1]) for r in cur.fetchall()]
    if window == "all":
        return TaskListResponse(tasks=tasks)
    return TaskListResponse(tasks=_windowed_tasks(tasks, window))


def _collect_updates(body: TaskUpdateRequest) -> tuple[list[str], list[object]]:
    """Validate the PATCH body and collect its SET clauses + params;
    raises HTTPException 422 on an invalid field."""
    sets: list[str] = []
    params: list[object] = []
    if body.status is not None:
        if body.status not in ("in_progress", "ongoing", "done", "cancelled"):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status: {body.status!r}. Must be one of: in_progress, ongoing, done, cancelled.",
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


def _patch_task_blocking(
    pool: ConnectionPool[Any], task_id: int, body: TaskUpdateRequest
) -> tuple[TaskRow, list[TaskOwnerNotification]]:
    """Run the task transaction and return committed owner-change notes.

    The route awaits task-note delivery after this function returns, so no
    network or process wake can roll back the owner assignment.
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

    with write_transaction(pool) as conn, conn.cursor() as cur:
        # The system root task is immutable (the task-tree anchor / default
        # parent) — reject any edit before writing, same rule as the SDK
        # update() path. Check existence here too so a missing task still 404s.
        cur.execute(
            "SELECT t.owner, t.is_root, a.status "
            "FROM agent_tasks t LEFT JOIN agents_meta a ON a.id = t.owner "
            "WHERE t.id = %s FOR UPDATE OF t",
            (task_id,),
        )
        guard = cur.fetchone()
        if guard is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found.")
        previous_owner, is_root, previous_owner_status = guard
        if is_root:
            raise HTTPException(
                status_code=422,
                detail=f"Task {task_id} is the system root task and is immutable.",
            )
        if body.status in ("done", "cancelled"):
            cur.execute(
                "SELECT id, count(*) OVER () FROM agent_tasks "
                "WHERE parent_id = %s AND status IN ('in_progress', 'ongoing') "
                "ORDER BY id LIMIT 1",
                (task_id,),
            )
            active_child = cur.fetchone()
            if active_child is not None:
                child_id, child_count = active_child
                raise HTTPException(
                    status_code=422,
                    detail=f"task {task_id} has {child_count} active child tasks "
                    f"(e.g. #{child_id}) — close or cancel them first",
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
        # A rename must keep the SDK create() invariant: no two in_progress
        # tasks share a title.
        if body.title is not None:
            cur.execute(
                "SELECT id, status FROM agent_tasks "
                "WHERE title = %s AND status = 'in_progress' AND id != %s LIMIT 1",
                (body.title, task_id),
            )
            dup = cur.fetchone()
            if dup is not None:
                raise HTTPException(
                    status_code=422,
                    detail=f"A task with title {body.title!r} already exists "
                    f"(task {dup[0]} is {dup[1]}); duplicate in_progress "
                    f"titles are not allowed.",
                )
        cur.execute(
            cast(
                LiteralString,
                f"WITH updated AS ( "  # noqa: S608
                f"    UPDATE agent_tasks AS t SET {', '.join(sets)}, updated_at = now() "
                f"    WHERE t.id = %s "
                f"    RETURNING {_TASK_COLS} "
                f") "
                f"SELECT u.*, a.label AS owner_label "
                f"FROM updated u "
                f"LEFT JOIN agents a ON u.owner = a.id",
            ),
            (*params, task_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found.")

    task = _row_to_task(row[:-1], "full", owner_label=row[-1])
    if "owner" not in body.model_fields_set or previous_owner == task.owner:
        return task, []
    return (
        task,
        owner_change_notifications(
            task.id,
            task.title,
            previous_owner,
            task.owner,
            actor=None,
            previous_owner_terminated=previous_owner_status in (None, "terminated"),
        ),
    )


@router.patch("/api/tasks/{task_id}")
async def patch_task(task_id: int, body: TaskUpdateRequest, request: Request) -> TaskRow:
    """Partially update a task; omitted fields stay unchanged.

    status, priority, title, description, and results are taken when non-null
    (priority must be one of P0..P3; ongoing marks long-running active work; a
    title colliding with another in_progress task's is rejected). owner reassigns to another
    agent (an explicit null is rejected — a task cannot be released).
    remind_interval_seconds must be a positive number of seconds <= 24h (an explicit
    null is rejected — reminders cannot be disabled). Any write resets the
    reminder counters, same as the SDK update path. An owner reassignment sends
    the SDK-equivalent task system notes after the database write commits.

    The system root task is immutable: any PATCH targeting it is rejected with
    422 (mirrors the SDK update() guard), so the task-tree anchor can never be
    reassigned, completed, cancelled, or otherwise edited.

    A status change to done or cancelled is rejected with 422 while any direct
    child remains active (in progress or ongoing). Close or cancel those children first.
    """
    task, notes = await asyncio.to_thread(
        _patch_task_blocking, request.app.state.db_pool, task_id, body
    )
    for note in notes:
        await post_agent_system_note(
            note.agent_id,
            SystemNoteIn(
                content=note.content,
                source="user",
                task_id=task_id if note.resurrect else None,
                resurrect=note.resurrect,
            ),
            request,
        )
    return task
