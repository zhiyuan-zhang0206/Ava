"""Task registry transition rules -- the single owner of the registry's write
invariants shared by the SDK (`ava_builtins/plugins/ava_fleet/task_registry.py`,
`_task_update.py`) and the gateway (`gateway/routers/tasks.py`): what counts as
closed, whether a task may close while a direct child is still open, and
whether a title is already held by an in_progress task. Both write surfaces
call these functions instead of hand-coding the same SQL and status literals,
so the two surfaces cannot drift apart -- same precedent as
`base/agents/tasks/reparent.py`, which also calls `is_closed` here for its own
closed-parent check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from base.agents.tasks.status import TaskStatus

if TYPE_CHECKING:
    # Annotation-only; keeping the import out of the module graph lets the fleet
    # plugin autoload stay off psycopg (task #3816).
    import psycopg

CLOSED_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.DONE, TaskStatus.CANCELLED})


def is_closed(status: str | None) -> bool:
    """True when `status` is done or cancelled; None (no status change) is never closed."""
    return status in CLOSED_STATUSES


def first_open_child(cur: psycopg.Cursor, task_id: int) -> tuple[int, int] | None:
    """Lowest-id in_progress direct child of `task_id`, with the open-child count, or None."""
    cur.execute(
        "SELECT id, count(*) OVER () FROM agent_tasks "
        "WHERE parent_id = %s AND status = %s "
        "ORDER BY id LIMIT 1",
        (task_id, TaskStatus.IN_PROGRESS.value),
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row is not None else None


def open_title_holder(
    cur: psycopg.Cursor, title: str, *, exclude_id: int | None = None
) -> tuple[int, str] | None:
    """The in_progress task holding `title` (other than `exclude_id`, if given), or None."""
    if exclude_id is None:
        cur.execute(
            "SELECT id, status FROM agent_tasks WHERE title = %s AND status = %s LIMIT 1",
            (title, TaskStatus.IN_PROGRESS.value),
        )
    else:
        cur.execute(
            "SELECT id, status FROM agent_tasks "
            "WHERE title = %s AND status = %s AND id != %s LIMIT 1",
            (title, TaskStatus.IN_PROGRESS.value, exclude_id),
        )
    row = cur.fetchone()
    return (row[0], row[1]) if row is not None else None
