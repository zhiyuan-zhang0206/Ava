"""Shared reparent validation for agent_tasks — one implementation for the two
write surfaces (SDK `update()` and gateway `PATCH /api/tasks/{id}`).

Both accept a `parent_id` change. The checks are identical: the parent must
exist, must not be the task itself, and must not be one of its own descendants
(no cycles); an explicit `None` resolves to the system root task (the same
anchor `create(parent=None)` uses). Kept here so the two surfaces cannot drift
apart.
"""

from __future__ import annotations

import psycopg


def system_root_id(cur: psycopg.Cursor) -> int | None:
    """Id of the system root task (task-tree anchor / default parent), or None
    when no is_root row exists yet (uninitialized table; prod always has one)."""
    cur.execute("SELECT id FROM agent_tasks WHERE is_root ORDER BY id LIMIT 1")
    row = cur.fetchone()
    return row[0] if row is not None else None


def resolve_reparent(cur: psycopg.Cursor, task_id: int, parent_id: int | None) -> int | None:
    """Validate a reparent and return the effective parent id to write.

    `parent_id=None` moves the task under the system root. Raises ValueError
    with a human-readable reason on a missing parent, self-parenting, or a
    cycle (the task moved under one of its own descendants). The caller holds
    the task row FOR UPDATE (read -> write race safety).
    """
    effective = system_root_id(cur) if parent_id is None else parent_id
    if effective is not None:
        cur.execute("SELECT id FROM agent_tasks WHERE id = %s", (effective,))
        if cur.fetchone() is None:
            raise ValueError(f"parent task {effective} does not exist")
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
