"""Shared reparent validation for agent_tasks — one implementation for the two
write surfaces (SDK `update()` and gateway `PATCH /api/tasks/{id}`).

Both accept a `parent_id` change. The checks are identical: the parent must
exist, must not be closed (done / cancelled -- a closed task never gains
children), must not be the task itself, and must not be one of its own
descendants (no cycles); an explicit `None` resolves to the system root task
(the anchor `create()` requires callers to pass explicitly -- id 1 -- for
top-level tasks). Kept here so the two surfaces cannot drift apart.
"""

from __future__ import annotations

import psycopg


def system_root_id(cur: psycopg.Cursor) -> int | None:
    """Id of the system root task (task-tree anchor / parent of top-level tasks), or None
    when no is_root row exists yet (uninitialized table; prod always has one)."""
    cur.execute("SELECT id FROM agent_tasks WHERE is_root ORDER BY id LIMIT 1")
    row = cur.fetchone()
    return row[0] if row is not None else None


def resolve_reparent(cur: psycopg.Cursor, task_id: int, parent_id: int | None) -> int | None:
    """Validate a reparent and return the effective parent id to write.

    `parent_id=None` moves the task under the system root. Raises ValueError
    with a human-readable reason on a missing parent, a closed (done /
    cancelled) parent, self-parenting, or a cycle (the task moved under one of
    its own descendants). The caller holds the task row FOR UPDATE (read ->
    write race safety); the parent row is locked FOR UPDATE here too, so a
    concurrent close cannot slip between this status read and the parent_id
    UPDATE (TOCTOU, QA #993).
    """
    effective = system_root_id(cur) if parent_id is None else parent_id
    if effective is not None:
        cur.execute("SELECT id, status FROM agent_tasks WHERE id = %s FOR UPDATE", (effective,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"parent task {effective} does not exist")
        if row[1] in ("done", "cancelled"):
            raise ValueError(
                f"task {task_id} cannot be moved under a closed parent "
                f"#{effective} ({row[1]}) — a closed task never gains children; "
                "reopen it or pass None to move under the system root"
            )
    if effective == task_id:
        raise ValueError(f"task {task_id} cannot be its own parent")
    node = effective
    while node is not None:
        cur.execute("SELECT parent_id FROM agent_tasks WHERE id = %s", (node,))
        row = cur.fetchone()
        node = row[0] if row else None
        if node == task_id:
            raise ValueError(
                f"task {task_id} cannot be moved under its own descendant #{effective}"
            )
    return effective
