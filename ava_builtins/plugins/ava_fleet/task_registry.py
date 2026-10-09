"""Task tracking — shared, persistent work items. Any agent can update any
task; owners are reminded periodically, and a task may nest under a parent.
"""

from __future__ import annotations

import builtins
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

import ava
import ava.sdk_surface.agent_identity
from ava.sdk_surface.validation import coerce_str, coerce_typed
from base.agents.context import AvaContext
from base.agents.tasks.model import TASK_COLUMNS, task_from_row
from base.agents.tasks.model import Task as Task
from base.agents.tasks.owner_notifications import TaskOwnerNotification, owner_change_notifications
from base.agents.tasks.reparent import resolve_reparent

if TYPE_CHECKING:
    # Annotation-only here (cursor params); the runtime import sits at the raise
    # site so plugin autoload stays off the psycopg stack (task #3816).
    import psycopg

    from base.telemetry import Event

from ._task_update import (
    _DEFAULT_PRIORITY,
    _STATUSES,
    _UNSET,
    _collect_update_fields,
    _nothing_to_update,
    _owner_actually_changed,
    _Unset,
    _validate_status,
    _write_task_update,
)

# `list` / `get` shadow builtins intentionally: these are the agent-facing names
# (ava.tasks.list / ava.tasks.get), matching ava.shell.sessions.list. flake8-builtins
# (`A`) is not in this repo's ruff select, and the SDK renderer reads annotations
# as strings without evaluating them, so the shadow is runtime-cosmetic. It is
# NOT invisible to a type checker, though: within this module the name `list`
# resolves to the function, so annotations that mean the builtin container are
# spelled `builtins.list[...]`.
__all_for_ava__ = ["Task", "create", "create_and_assign", "get", "list", "log", "update"]


def create(
    title: str,
    description: str,
    *,
    parent: int,
    remind_interval_seconds: int | None = None,
    owner: int | None = None,
    priority: str = _DEFAULT_PRIORITY,
    operation_key: str | None = None,
) -> Task:
    """Args:
    title: unique among in_progress tasks.
    parent: id of an existing, open task this task descends from; the system
        root (id 1) parents only top-level tasks. An invalid parent raises ValueError.
    remind_interval_seconds: cannot be disabled; None = the priority default
        (P0 30m / P1 1h / P2 2h / P3 4h), capped at 24h.
    owner: agent to assign to; None means you.
    priority: "P0" (highest) through "P3" (lowest).
    operation_key: reuse the same key and inputs to return the original Task.
        The returned snapshot may be outdated; use get(task.id) for current state.
    """
    return _create(
        ava.context,
        title,
        description,
        parent=parent,
        remind_interval_seconds=remind_interval_seconds,
        owner=owner,
        priority=priority,
        operation_key=operation_key,
    )


def _create(
    context: AvaContext,
    title: str,
    description: str,
    *,
    parent: int,
    remind_interval_seconds: int | None = None,
    owner: int | None = None,
    priority: str = _DEFAULT_PRIORITY,
    operation_key: str | None = None,
) -> Task:
    """Apply create with one explicit context, including admission revalidation."""
    from base.agents.tasks.creation import create_task_in_transaction
    from base.api_contracts.idempotency import validate_idempotency_key

    from ._task_creation_receipts import record_creation, replay_creation

    if operation_key is not None:
        operation_key = validate_idempotency_key(operation_key)
    title = coerce_str(title, "title")
    description = coerce_str(description, "description")
    parent = coerce_typed(parent, "parent", int)
    remind_interval_seconds = coerce_typed(
        remind_interval_seconds, "remind_interval_seconds", int, allow_none=True
    )
    owner = coerce_typed(owner, "owner", int, allow_none=True)
    priority = coerce_str(priority, "priority")
    actor = ava.sdk_surface.agent_identity.require_agent_id(context)
    effective_owner = owner if owner is not None else actor
    request: dict[str, object] = {
        "title": title,
        "description": description,
        "parent": parent,
        "owner": effective_owner,
        "priority": priority,
        "remind_interval_seconds": remind_interval_seconds,
    }
    with context.sql.transaction(), context.sql.cursor() as cur:
        snapshot = replay_creation(cur, actor, operation_key, request, context=context)
        if snapshot is not None:
            return Task(**snapshot)
        task, created_event, note_events = create_task_in_transaction(
            cur,
            title,
            description,
            parent=parent,
            owner=effective_owner,
            actor=actor,
            priority=priority,
            remind_interval_seconds=remind_interval_seconds,
        )
        record_creation(cur, actor, operation_key, request, asdict(task))
    from base import telemetry  # deferred (task #3816)

    telemetry.emit_prepared(created_event)

    for event in note_events:
        telemetry.emit_prepared(event)

    # Live-refresh every open task board (fleet-wide invalidate + refetch).
    from base.events.live import announce, bus  # deferred (task #3816)

    announce.publish_task_created_sync(bus.EventBus.from_settings(), actor, task.id)
    return task


def create_and_assign(
    title: str,
    description: str,
    *,
    operation_key: str,
    preset: str | None = None,
    label: str | None = None,
    config_overlay: dict[str, Any] | None = None,
    machine: str | None = None,
    parent: int,
    remind_interval_seconds: int | None = None,
    priority: str = _DEFAULT_PRIORITY,
) -> tuple[Task, int]:
    """Create an agent and its assigned task under one operation key.

    Reuse the key and inputs to recover the original pair after a lost reply.
    The returned pair proves acceptance; query task and agent state to observe
    current progress. Borrowed leases are unsupported.

    Arguments have the same meaning as in create() and ava.agents.spawn().
    With no preset selected, current defaults apply; machine defaults to your
    own machine. The parent must exist and be open before either object is born.

    Returns:
        (task, agent_id).
    """
    from base.api_contracts.idempotency import validate_idempotency_key

    from ._task_assignment import create_and_assign_guarded

    operation_key = validate_idempotency_key(operation_key)
    title = coerce_str(title, "title")
    description = coerce_str(description, "description")
    preset = coerce_str(preset, "preset", allow_none=True)
    label = coerce_str(label, "label", allow_none=True)
    config_overlay = coerce_typed(config_overlay, "config_overlay", dict, allow_none=True)
    machine = coerce_str(machine, "machine", allow_none=True)
    parent = coerce_typed(parent, "parent", int)
    remind_interval_seconds = coerce_typed(
        remind_interval_seconds, "remind_interval_seconds", int, allow_none=True
    )
    priority = coerce_str(priority, "priority")
    return create_and_assign_guarded(
        title,
        description,
        parent=parent,
        preset=preset,
        label=label,
        config_overlay=config_overlay,
        machine=machine,
        remind_interval_seconds=remind_interval_seconds,
        priority=priority,
        operation_key=operation_key,
    )


def update(
    task_id: int,
    *,
    status: str | None = None,
    title: str | None = None,
    description: str | None = None,
    results: str | None = None,
    owner: int | None = _UNSET,  # type: ignore[assignment]
    remind_interval_seconds: int | None = _UNSET,  # type: ignore[assignment]
    priority: str | None = None,
    parent_id: int | None = _UNSET,  # type: ignore[assignment]
    note: str | None = None,
    operation_key: str | None = None,
) -> None:
    """Any write resets the reminder clock. Owner changes notify both owners; other
    updates tell the owner who changed it; a parent-only reparent stays silent.

    Args:
        status: one of "in_progress", "done", "cancelled"; closing is rejected
            while any direct child is in progress.
        results: replaces the whole field; use note to append instead.
        owner: agent id to reassign to; a task always has an owner.
        remind_interval_seconds: None = unchanged; reminders cannot be disabled; capped at 24h.
        parent_id: reparent (explicit None = system root; int = set parent).
        operation_key: optional stable key for this agent and task; reuse it to
            replay a committed update without another note or notification.
            Different effective fields with the same key raise ValueError.
    """
    _update(
        ava.context,
        task_id,
        status=status,
        title=title,
        description=description,
        results=results,
        owner=owner,
        remind_interval_seconds=remind_interval_seconds,
        priority=priority,
        parent_id=parent_id,
        note=note,
        operation_key=operation_key,
    )


def _update_value(value: int | _Unset | None) -> int | None:
    """Normalize the two unchanged spellings at typed business-call boundaries."""
    return None if isinstance(value, _Unset) else value


def _update(
    context: AvaContext,
    task_id: int,
    *,
    status: str | None = None,
    title: str | None = None,
    description: str | None = None,
    results: str | None = None,
    owner: int | _Unset | None = _UNSET,
    remind_interval_seconds: int | _Unset | None = _UNSET,
    priority: str | None = None,
    parent_id: int | _Unset | None = _UNSET,
    note: str | None = None,
    operation_key: str | None = None,
) -> None:
    """Apply update with one explicit context, including admission revalidation."""
    task_id = coerce_typed(task_id, "task_id", int)
    status = coerce_str(status, "status", allow_none=True)
    title = coerce_str(title, "title", allow_none=True)
    description = coerce_str(description, "description", allow_none=True)
    results = coerce_str(results, "results", allow_none=True)
    if owner is not _UNSET:
        owner = coerce_typed(owner, "owner", int, allow_none=True)
    if remind_interval_seconds is not _UNSET:
        remind_interval_seconds = coerce_typed(
            remind_interval_seconds, "remind_interval_seconds", int, allow_none=True
        )
    priority = coerce_str(priority, "priority", allow_none=True)
    if parent_id is not _UNSET:
        parent_id = coerce_typed(parent_id, "parent_id", int, allow_none=True)
    note = coerce_str(note, "note", allow_none=True)
    _validate_status(status)

    sets, params, payload, owner_changing, changes = _collect_update_fields(
        task_id,
        status,
        title,
        description,
        results,
        _update_value(owner),
        _update_value(remind_interval_seconds),
        priority,
    )
    if _nothing_to_update(sets, note) and parent_id is _UNSET:
        raise ValueError(
            "update needs at least one of status, title, description, results, "
            "owner, remind_interval_seconds, priority, parent_id, note to change"
        )
    # A parent-only reparent (no business field changed, no note) is structural
    # tree maintenance — moving a task between parents, not changing its work —
    # so it must not notify — and therefore must not resurrect — the owner.
    # Batch reparenting used to fire one owner notification per move, each
    # auto-resurrecting a terminated owner (62-agent wake storm, 2026-08-27).
    parent_only = parent_id is not _UNSET and _nothing_to_update(sets, note)
    # Reset the reminder counters on any update — a fresh overdue window starts
    # from this update, so any previous reminder is stale. The delegator
    # escalation marker goes with them: clearing it lets the fresh window
    # escalate on its own.
    sets.append("last_reminded_at = NULL")
    sets.append("reminder_count = 0")
    sets.append("escalated_at = NULL")

    from ._task_receipts import record_update, replay_update, update_identity, update_request

    actor, operation_key = update_identity(operation_key, context=context)
    request_body = update_request(
        {
            "status": status,
            "title": title,
            "description": description,
            "results": results,
            "owner": owner,
            "remind_interval_seconds": remind_interval_seconds,
            "priority": priority,
            "parent_id": parent_id,
            "note": note,
        }
    )
    with context.sql.transaction(), context.sql.cursor() as cur:
        if replay_update(cur, actor, task_id, operation_key, request_body, context=context):
            return
        if parent_id is not _UNSET:
            sets.append("parent_id = %s")
            params.append(resolve_reparent(cur, task_id, cast(int | None, parent_id)))
        old_owner, current_title, new_owner, updated_event = _write_task_update(
            cur,
            task_id,
            status,
            sets,
            params,
            payload,
            changes,
            title,
            note,
            _update_value(owner),
            owner_changing,
            actor,
        )
        if parent_id is not _UNSET:
            changes.append("parent → root" if parent_id is None else f"parent → #{parent_id}")
        note_events = _queue_after_update(
            cur,
            task_id,
            title if title is not None else current_title,
            old_owner,
            new_owner,
            actor,
            changes,
            owner_changed=_owner_actually_changed(owner_changing, old_owner, new_owner),
            parent_only=parent_only,
        )
        record_update(cur, actor, task_id, operation_key, request_body)

    from base import telemetry  # deferred (task #3816)

    telemetry.emit_prepared(updated_event)

    # Agent-scoped side effects run after the row change commits: telling an
    # agent auto-wakes it, so keep it out of the transaction. System tooling
    # has no actor for a task note or TaskUpdated; like gateway PATCH, its
    # committed write relies on the board's normal poll.
    if actor is not None:  # agent_id() is None before bootstrap; system tooling has no actor.
        for event in note_events:
            telemetry.emit_prepared(event)
        from base.events.live import announce, bus  # deferred (task #3816)

        announce.publish_task_updated_sync(bus.EventBus.from_settings(), actor, task_id)


def _queue_after_update(
    cur: psycopg.Cursor,
    task_id: int,
    title: str,
    old_owner: int | None,
    new_owner: int | None,
    actor: int | None,
    changes: builtins.list[str],
    *,
    owner_changed: bool,
    parent_only: bool,
) -> builtins.list[Event]:
    """Enqueue policy-approved updates before the task transaction commits."""
    if owner_changed:
        from base.agents.tasks.delivery import supersede_task_assignments

        supersede_task_assignments(cur, task_id)
    if actor is None or parent_only:
        return []
    if owner_changed:
        return _queue_owner_change(
            cur, task_id, title, old_owner, new_owner, actor, changes=changes
        )
    if new_owner is not None and actor != new_owner and not _is_terminated(cur, new_owner):
        return _queue_owner_updated(cur, task_id, title, new_owner, actor, changes)
    return []


def _queue_owner_change(
    cur: psycopg.Cursor,
    task_id: int,
    title: str,
    old_owner: int | None,
    new_owner: int | None,
    actor: int,
    description: str | None = None,
    changes: builtins.list[str] | None = None,
) -> builtins.list[Event]:
    """Commit assignment directions with their task mutation, never HTTP-send."""
    from base.agents.tasks.delivery import enqueue_task_notifications

    notes = owner_change_notifications(
        task_id,
        title,
        old_owner,
        new_owner,
        actor=actor,
        previous_owner_terminated=old_owner is None or _is_terminated(cur, old_owner),
        description=description,
        changes=changes,
    )
    receipts = enqueue_task_notifications(cur, task_id, notes, f"agent:{actor}")
    return [receipt.event for receipt in receipts if receipt.event is not None]


def _queue_owner_updated(
    cur: psycopg.Cursor,
    task_id: int,
    title: str,
    owner: int,
    actor: int,
    changes: builtins.list[str],
) -> builtins.list[Event]:
    """Queue an informational update while preserving the no-resurrection policy."""
    from base.agents.tasks.delivery import enqueue_task_notifications

    detail = "\n".join(f"- {change}" for change in changes)
    note = TaskOwnerNotification(
        owner,
        f'Task #{task_id} "{title}" was updated by agent #{actor}:\n{detail}',
        resurrect=False,
        link_task=True,
    )
    receipts = enqueue_task_notifications(cur, task_id, [note], f"agent:{actor}")
    return [receipt.event for receipt in receipts if receipt.event is not None]


def _is_terminated(cur: psycopg.Cursor, agent_id: int) -> bool:
    """True when an agent is gone -- terminated, or absent from agents_meta.

    Gates only the previous-owner notification: a terminated former owner is
    left asleep rather than resurrected just to be told a task left it."""
    cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
    meta = cur.fetchone()
    return meta is None or meta[0] == "terminated"


def log(task_id: int, message: str, *, operation_key: str | None = None) -> None:
    """Append one timestamped line; reuse operation_key to replay without appending again."""
    task_id = coerce_typed(task_id, "task_id", int)
    message = coerce_str(message, "message")
    if operation_key is None:
        update(task_id, note=message)
    else:
        update(task_id, note=message, operation_key=operation_key)


def get(task_id: int) -> Task:
    """Return the task with this id."""
    task_id = coerce_typed(task_id, "task_id", int)
    with ava.DB.cursor() as cur:
        cur.execute(f"SELECT {TASK_COLUMNS} FROM agent_tasks WHERE id = %s", (task_id,))  # noqa: S608
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"task {task_id} does not exist")
    return task_from_row(row)


def _where_clause(filters: builtins.list[str]) -> str:
    """' WHERE a AND b' for the given filters, or '' when there are none."""
    return (" WHERE " + " AND ".join(filters)) if filters else ""


def _build_list_query(
    parent: int | None,
    owner: int | None,
    status: str | None,
    recursive: bool,  # noqa: FBT001 — internal helper flag, always passed by name
) -> tuple[str, builtins.list[object]]:
    """SQL + params for list(): a plain filter query, or a recursive CTE over
    the whole subtree rooted at `parent`. parent_id always points at an older
    row (a child is created after its parent), so the tree cannot cycle and the
    recursion terminates. owner / status filter the subtree at the end."""
    filters: builtins.list[str] = []
    params: builtins.list[object] = []
    if owner is not None:
        filters.append("owner = %s")
        params.append(owner)
    if status is not None:
        filters.append("status = %s")
        params.append(status)

    if recursive and parent is not None:
        sql = (
            "WITH RECURSIVE subtree AS ("  # noqa: S608
            " SELECT * FROM agent_tasks WHERE parent_id = %s"
            " UNION ALL"
            " SELECT c.* FROM agent_tasks c JOIN subtree s ON c.parent_id = s.id"
            f") SELECT {TASK_COLUMNS} FROM subtree{_where_clause(filters)} ORDER BY created_at, id"
        )
        return sql, [parent, *params]

    if parent is not None:
        filters.append("parent_id = %s")
        params.append(parent)
    sql = f"SELECT {TASK_COLUMNS} FROM agent_tasks{_where_clause(filters)} ORDER BY created_at, id"  # noqa: S608
    return sql, params


def list(
    *,
    parent: int | None = None,
    owner: int | None = None,
    status: str | None = None,
    recursive: bool = False,
) -> builtins.list[Task]:
    """In creation order; no filters lists every task.

    Args:
        parent: keep only direct subtasks of this task; with recursive=True,
            its whole descendant subtree.
    """
    parent = coerce_typed(parent, "parent", int, allow_none=True)
    owner = coerce_typed(owner, "owner", int, allow_none=True)
    status = coerce_str(status, "status", allow_none=True)
    recursive = coerce_typed(recursive, "recursive", bool)
    if status is not None and status not in _STATUSES:
        raise ValueError(f"status must be one of {sorted(_STATUSES)}, got {status!r}")

    sql, params = _build_list_query(parent, owner, status, recursive)
    with ava.DB.cursor() as cur:
        cur.execute(sql, params)
        return [task_from_row(r) for r in cur.fetchall()]
