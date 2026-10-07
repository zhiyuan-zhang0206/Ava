"""Task creation effects inside a caller-owned transaction, without commit or wake."""

from typing import Any

import psycopg
from psycopg import sql

from base.agents.tasks.delivery import enqueue_task_notifications
from base.agents.tasks.model import TASK_COLUMNS, Task, task_from_row
from base.agents.tasks.owner_notifications import owner_change_notifications
from base.agents.tasks.priority import resolve_create_args
from base.agents.tasks.rules import is_closed, open_title_holder
from base.telemetry import Event


def ensure_parent_exists(cur: psycopg.Cursor[Any], parent: int) -> None:
    """Validate `parent` names an existing task; raise ValueError otherwise.

    Also rejects parent=1 on a deployment where task 1 is not the system root
    (a migrated database whose root carries another id): the documented root
    id (1) must not silently attach a top-level task under a different parent.
    A closed (done / cancelled) parent is rejected too: a closed task must
    never gain children -- tasks created after a parent closed are what
    produced the false-orphan rows in the task graph (task #1975). The system
    root is exempt by construction (its status is 'in_progress' — never closed).
    The parent row is locked FOR UPDATE: the close path holds the same lock,
    so a concurrent close cannot slip between this status read and the child
    INSERT (TOCTOU, QA #993).
    Runs inside the caller's transaction/cursor."""
    cur.execute("SELECT id, is_root, status FROM agent_tasks WHERE id = %s FOR UPDATE", (parent,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(
            f"parent task {parent} does not exist -- create the parent first, "
            "or pass the system root task id (1) for a top-level task"
        )
    if parent == 1 and not row[1]:
        cur.execute("SELECT id FROM agent_tasks WHERE is_root ORDER BY id LIMIT 1")
        root = cur.fetchone()
        what = f"task #{root[0]}" if root is not None else "unseeded (no root exists)"
        raise ValueError(
            f"task 1 is not the system root task -- the root is {what}; "
            "pass its id for a top-level task"
        )
    if is_closed(row[2]):
        raise ValueError(
            f"parent task {parent} is {row[2]} — a closed task cannot be the "
            "parent of a new task; reopen it or pass the system root task id "
            "(1) for a top-level task"
        )


def insert_task(
    cur: psycopg.Cursor[Any],
    title: str,
    description: str,
    effective_parent: int | None,
    effective_owner: int,
    remind_interval_seconds: int,
    priority: str,
    actor: int,
) -> tuple[Task, Event]:
    """INSERT a task row + record its create audit fact inside the caller's transaction.

    Returns the task and the recorded event; the caller emits the event after
    its transaction commits.

    Rejects duplicate in_progress titles -- prevents agents from creating
    the same task twice (#60, #253)."""
    existing = open_title_holder(cur, title)
    if existing is not None:
        raise ValueError(
            f"task with title {title!r} already exists (task #{existing[0]} is {existing[1]}) — "
            f"duplicate in_progress titles are not allowed"
        )
    query = sql.SQL(
        "INSERT INTO agent_tasks (parent_id, title, description, created_by, owner, "
        "remind_interval_seconds, priority) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {}"
    ).format(sql.SQL(", ").join(sql.Identifier(column) for column in TASK_COLUMNS.split(", ")))
    try:
        cur.execute(
            query,
            (
                effective_parent,
                title,
                description,
                str(actor),
                effective_owner,
                remind_interval_seconds,
                priority,
            ),
        )
    except psycopg.errors.UniqueViolation as exc:
        # The pre-check above is the common path; this catches the race where
        # two creates both pass it and the partial unique index
        # (agent_tasks_title_unique_in_progress) rejects the second insert. The
        # transaction is aborted, so no lookup is possible here — the message
        # is generic by design.
        raise ValueError(
            f"task with title {title!r} already exists among in_progress tasks — "
            "duplicate in_progress titles are not allowed (enforced by the database)"
        ) from exc
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("expected exactly one row: task insert")
    task = task_from_row(row)
    from base.telemetry.audit_events import prepare_event_log, record_audit  # deferred (task #3816)

    event = record_audit(
        cur.connection,
        prepare_event_log(
            event_type="task_create",
            agent_id=actor,
            source="self",
            payload={
                "task_id": task.id,
                "title": title,
                "parent_id": effective_parent,
                "owner": effective_owner,
                "remind_interval_seconds": remind_interval_seconds,
                "priority": priority,
            },
        ),
    )
    return task, event


def queue_creation_notifications(
    cur: psycopg.Cursor[Any],
    task: Task,
    owner: int,
    actor: int,
    description: str,
) -> list[Event]:
    """Queue the existing new-owner policy in the task producer's transaction."""
    notes = owner_change_notifications(
        task.id,
        task.title,
        None,
        owner,
        actor=actor,
        previous_owner_terminated=True,
        description=description,
    )
    receipts = enqueue_task_notifications(cur, task.id, notes, f"agent:{actor}")
    return [receipt.event for receipt in receipts if receipt.event is not None]


def create_task_in_transaction(
    cur: psycopg.Cursor[Any],
    title: str,
    description: str,
    *,
    parent: int,
    owner: int,
    actor: int,
    priority: str,
    remind_interval_seconds: int | None,
) -> tuple[Task, Event, list[Event]]:
    """Write parent-validated task, audit and notifications without committing."""
    interval, priority = resolve_create_args(remind_interval_seconds, priority)
    ensure_parent_exists(cur, parent)
    task, event = insert_task(cur, title, description, parent, owner, interval, priority, actor)
    notes = queue_creation_notifications(cur, task, owner, actor, description)
    return task, event, notes
