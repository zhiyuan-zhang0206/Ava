"""Task tracking — shared, persistent work items. All agents are peers: any
agent can update any task; owners are reminded periodically; a task may have
a parent."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import TypeGuard

import psycopg

import ava
import ava._boot
import ava.agents
from ava._sdk_validation import coerce_str, coerce_typed
from shared.audit_events import insert_event_log
from shared.live_announce import publish_task_created_sync, publish_task_updated_sync
from shared.priority import DEFAULT_REMIND_INTERVAL_SECONDS, Priority, validate_priority
from shared.task_notes import task_note_line
from shared.task_reparent import resolve_reparent
from shared.task_timestamps import render_task_timestamps

# `list` / `get` shadow builtins intentionally: these are the agent-facing names
# (ava.tasks.list / ava.tasks.get), matching ava.shell.sessions.list. flake8-builtins
# (`A`) is not in this repo's ruff select, and the SDK renderer reads annotations
# as strings without evaluating them, so the shadow is runtime-cosmetic. It is
# NOT invisible to a type checker, though: within this module the name `list`
# resolves to the function, so annotations that mean the builtin container are
# spelled `builtins.list[...]`.
__all_for_ava__ = ["Task", "create", "create_and_assign", "get", "list", "log", "update"]

# The three statuses an agent can assign to a task. 'ongoing' is deliberately NOT
# here: it is the system root task's permanent state (schema CHECK + DB constraint
# agent_tasks_root_status_ongoing), set only by the schema seed / migration, never
# by create()/update() -- the root itself is immutable (see _write_task_update),
# and a non-root task must never be 'ongoing'. list(status=...) additionally
# accepts 'ongoing' (a read filter, so the root is addressable by status there).
# A task is born 'in_progress' (the DB default; user ruling 2026-08-29 -- the
# 'open' status was meaningless, creation starts the work immediately).
_STATUSES = frozenset({"in_progress", "done", "cancelled"})

# The stakes axis of a task (P0 highest .. P3 lowest) — same four rungs as a
# notice, both validated against the shared Priority enum. Orders the board
# within a status column; a stalled task's escalation notice inherits it.
_DEFAULT_PRIORITY = "P2"


# A new task reminds its owner after a silence window that scales with its
# priority (P0 30m / P1 1h / P2 2h / P3 4h — shared.priority.DEFAULT_REMIND_INTERVAL_SECONDS).
# An unattended in-progress task is the common failure the reminder guards
# against, so the reminder is always on and cannot be disabled —
# create(remind_interval_seconds=None) falls back to the priority default
# rather than turning it off; an explicit value always wins.

# Reminders cannot be turned off, so the interval is capped at 24h: every task
# gets at least one reminder a day. Enforced on every SDK write (create / update)
# and mirrored on the gateway PATCH path.
_MAX_REMIND_INTERVAL_SECONDS = 86400


def _validate_remind_interval_seconds(seconds: int) -> None:
    """Reject a remind_interval_seconds that is not a positive number of seconds <= 24h.

    Reminders cannot be disabled, so 0 / negative (which would remind on every
    sweep) and values over the 24h cap are both refused."""
    if not 0 < seconds <= _MAX_REMIND_INTERVAL_SECONDS:
        raise ValueError(
            f"remind_interval_seconds must be a positive number of seconds <= {_MAX_REMIND_INTERVAL_SECONDS} "
            f"(24h) -- reminders cannot be disabled, got {seconds!r}"
        )


# Column order matches the Task field order and the Task(*row) unpacking in
# _row_to_task; keep the three aligned.
_COLS = "id, parent_id, title, description, results, status, owner, created_by, created_at, updated_at, remind_interval_seconds, last_reminded_at, reminder_count, priority"


class _Unset:
    """Sentinel for update() keyword defaults: tells 'argument not passed'
    (leave the field as-is). Both the sentinel and None mean 'do not change'
    for owner and remind_interval_seconds; only an explicit value triggers a write.
    Tasks must always have an owner, so owner=None in update() no longer
    releases a task — it is treated as 'no change'. Reminders cannot be
    disabled either, so remind_interval_seconds=None is likewise 'no change' (an
    explicit interval, capped at 24h, is the only way to change it)."""

    def __repr__(self) -> str:
        return "<unchanged>"


_UNSET = _Unset()


@dataclass
class Task:
    """remind_interval_seconds is seconds without updates before the owner is
    reminded; reminders cannot be disabled."""

    id: int
    parent_id: int | None
    title: str
    description: str
    results: str | None
    status: str
    owner: int | None
    created_by: str
    created_at: str
    updated_at: str
    remind_interval_seconds: int | None = None
    last_reminded_at: str | None = None
    reminder_count: int = 0
    priority: str = _DEFAULT_PRIORITY

    @property
    def brief(self) -> str:
        """Deprecated alias for description."""
        return self.description

    @property
    def content(self) -> str | None:
        """Deprecated alias for results."""
        return self.results

    def __str__(self) -> str:
        owner = f"owner=#{self.owner}" if self.owner is not None else "unowned"
        parent = f" parent=#{self.parent_id}" if self.parent_id is not None else ""
        return f"#{self.id} [{self.status}] {self.title}  {owner}{parent}"


def _row_to_task(row: tuple) -> Task:
    """Build a Task from a _COLS row; timestamps rendered bare (issue #181)."""
    return Task(*render_task_timestamps(row, _COLS))


def _ensure_parent_exists(cur: psycopg.Cursor, parent: int) -> None:
    """Validate `parent` names an existing task; raise ValueError otherwise.

    Also rejects parent=1 on a deployment where task 1 is not the system root
    (a migrated database whose root carries another id): the documented root
    id (1) must not silently attach a top-level task under a different parent.
    Runs inside the caller's transaction/cursor."""
    cur.execute("SELECT id, is_root FROM agent_tasks WHERE id = %s", (parent,))
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


def _insert_task(
    cur: psycopg.Cursor,
    title: str,
    description: str,
    effective_parent: int | None,
    effective_owner: int,
    remind_interval_seconds: int,
    priority: str,
    actor: int,
) -> Task:
    """INSERT a task row + its create event log inside the caller's transaction.

    Rejects duplicate in_progress titles -- prevents agents from creating
    the same task twice (#60, #253)."""
    cur.execute(
        "SELECT id, status FROM agent_tasks WHERE title = %s AND status = 'in_progress' LIMIT 1",
        (title,),
    )
    existing = cur.fetchone()
    if existing is not None:
        raise ValueError(
            f"task with title {title!r} already exists (task #{existing[0]} is {existing[1]}) — "
            f"duplicate in_progress titles are not allowed"
        )
    sql = f"INSERT INTO agent_tasks (parent_id, title, description, created_by, owner, remind_interval_seconds, priority) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_COLS}"  # noqa: S608
    try:
        cur.execute(
            sql,
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
    task = _row_to_task(row)
    insert_event_log(
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
    )
    return task


def create(
    title: str,
    description: str | None = None,
    *,
    parent: int,
    remind_interval_seconds: int | None = None,
    owner: int | None = None,
    priority: str = _DEFAULT_PRIORITY,
    brief: str | None = None,
) -> Task:
    """
    Args:
        title: unique among in_progress tasks.
        parent: id of an existing task this task descends from. The system
            root task (id 1) parents only top-level tasks; every other task
            must reference an existing task. Raises ValueError when the
            parent does not exist (or, for parent=1, when task 1 is not the
            system root on this deployment).
        remind_interval_seconds: cannot be disabled; None means the priority
            default (P0 30m / P1 1h / P2 2h / P3 4h), capped at 24h; an
            explicit value wins over the default.
        owner: agent to assign to; None means you. An owner other than you is
            notified.
        priority: "P0" (highest) through "P3" (lowest).
        brief: deprecated alias for description.
    """
    title = coerce_str(title, "title")
    description = coerce_str(description, "description", allow_none=True)
    parent = coerce_typed(parent, "parent", int)
    remind_interval_seconds = coerce_typed(
        remind_interval_seconds, "remind_interval_seconds", int, allow_none=True
    )
    owner = coerce_typed(owner, "owner", int, allow_none=True)
    priority = coerce_str(priority, "priority")
    brief = coerce_str(brief, "brief", allow_none=True)
    description, remind_interval_seconds, priority = _resolve_create_args(
        brief, description, remind_interval_seconds, priority
    )
    actor = ava._boot.agent_id()
    effective_owner = owner if owner is not None else actor
    with ava.DB.transaction(), ava.DB.cursor() as cur:
        # parent is required: only the system root task (id 1) may parent the
        # deployment's top-level tasks; every other task must name an existing
        # task as its parent. Validate here for a friendly error instead of a
        # raw foreign-key violation from the INSERT.
        _ensure_parent_exists(cur, parent)
        task = _insert_task(
            cur,
            title,
            description,
            parent,
            effective_owner,
            remind_interval_seconds,
            priority,
            actor,
        )

    # Notify the assigned owner when it differs from the creator — same
    # post-transaction pattern as update() to avoid waking an agent inside a
    # transaction. The new owner is told even when terminated (the system-note
    # delivery auto-resurrects it), so an assigned task never strands on a dead
    # agent.
    if owner is not None and owner != actor:
        _notify_owner_change(task.id, title, None, owner, actor, description=description)

    # Live-refresh every open task board (fleet-wide invalidate + refetch).
    publish_task_created_sync(actor, task.id)
    return task


def create_and_assign(
    title: str,
    description: str,
    *,
    preset: str = "coder",
    label: str | None = None,
    config_overlay: dict | None = None,
    machine: str | None = None,
    parent: int,
    remind_interval_seconds: int | None = None,
    priority: str = _DEFAULT_PRIORITY,
) -> tuple[Task, int]:
    """Spawn an agent and assign it a task in one call.

    The new agent receives the task id, title, and description as its first
    message; arguments carry the same meaning as in create() and
    ava.agents.spawn(). ``machine`` defaults to your own machine.
    ``parent``: same rule as create(). The parent is validated before the
    agent spawns.

    Returns:
        (task, agent_id).
    """
    title = coerce_str(title, "title")
    description = coerce_str(description, "description")
    preset = coerce_str(preset, "preset")
    label = coerce_str(label, "label", allow_none=True)
    config_overlay = coerce_typed(config_overlay, "config_overlay", dict, allow_none=True)
    machine = coerce_str(machine, "machine", allow_none=True)
    parent = coerce_typed(parent, "parent", int)
    remind_interval_seconds = coerce_typed(
        remind_interval_seconds, "remind_interval_seconds", int, allow_none=True
    )
    priority = coerce_str(priority, "priority")
    # 0. Validate the parent before spawning: create() would reject a bad
    # parent after the agent exists, leaving an orphaned agent behind.
    with ava.DB.transaction(), ava.DB.cursor() as cur:
        _ensure_parent_exists(cur, parent)

    # 1. Spawn the agent — must exist before task creation so it can be the owner.
    agent_id = ava.agents.spawn(
        preset=preset,
        label=label,  # pyright: ignore[reportCallIssue] — fleet plugin wraps spawn with label
        config_overlay=config_overlay,
        machine=machine,
    )

    # 2. Create the task with the spawned agent as owner — create() sends the
    # notification with task id, title, and description.
    task = create(
        title=title,
        description=description,
        parent=parent,
        remind_interval_seconds=remind_interval_seconds,
        owner=agent_id,
        priority=priority,
    )

    # 3. Return both so the caller can track the task and the agent.
    return task, agent_id


def _append_note_to_results(cur, task_id: int, note: str) -> None:  # noqa: ANN001
    """Append a timestamped note line to a task's results within the
    current transaction.  Caller must hold FOR UPDATE on the row."""
    line = task_note_line(note)
    cur.execute(
        "SELECT results FROM agent_tasks WHERE id = %s FOR UPDATE",
        (task_id,),
    )
    current = cur.fetchone()[0] or ""
    if current and not current.endswith("\n"):
        current += "\n"
    cur.execute(
        "UPDATE agent_tasks SET results = %s WHERE id = %s",
        (current + line, task_id),
    )


def _resolve_update_args(
    status: str | None,
    content: str | None,
    results: str | None,
) -> tuple[str | None, str | None]:
    """Validate the status rung and resolve the deprecated content alias;
    returns (status, results)."""
    if status is not None and status not in _STATUSES:
        if status == "ongoing":
            raise ValueError(
                "'ongoing' is the system root task's permanent state and cannot be "
                "assigned via update() -- the root task itself is immutable"
            )
        raise ValueError(f"status must be one of {sorted(_STATUSES)}, got {status!r}")
    if content is not None:
        if results is not None:
            raise TypeError("pass results only; content is a deprecated alias")
        results = content
    return status, results


def _nothing_to_update(sets: builtins.list[str], note: str | None) -> bool:
    """True when update() received no field to change and no note to append."""
    return not sets and note is None


def _owner_actually_changed(
    owner_changing: bool,  # noqa: FBT001 — internal helper flag, always passed by name
    old_owner: int | None,
    new_owner: int | None,
) -> bool:
    """True when the reassignment is real (an explicit owner differing from the
    current one) -- gates the post-commit notification."""
    return owner_changing and old_owner != new_owner


def _resolve_create_args(
    brief: str | None,
    description: str | None,
    remind_interval_seconds: int | None,
    priority: str,
) -> tuple[str, int, str]:
    """Resolve the deprecated brief alias, require a description, and apply the
    reminder / priority validation rules. Returns (description,
    remind_interval_seconds, priority)."""
    if brief is not None:
        if description is not None:
            raise TypeError("pass description only; brief is a deprecated alias")
        description = brief
    if description is None:
        raise TypeError("create() missing required argument: 'description'")
    validate_priority(priority)
    # Reminders cannot be turned off: None means "use the priority default",
    # not "off". The interval scales with stakes — a P0 task nags its owner
    # after 30 minutes of silence, a P3 task only after 4 hours.
    if remind_interval_seconds is None:
        remind_interval_seconds = DEFAULT_REMIND_INTERVAL_SECONDS[Priority(priority)]
    _validate_remind_interval_seconds(remind_interval_seconds)
    return description, remind_interval_seconds, priority


def _owner_is_changing(owner: int | None) -> bool:
    """True when update() received an explicit new owner. owner=None now means
    "no change" (same as not passing it), not "release"; only an explicit agent
    id triggers a reassignment."""
    return owner is not _UNSET and owner is not None


def _collect_update_fields(
    task_id: int,
    status: str | None,
    title: str | None,
    description: str | None,
    results: str | None,
    owner: int | None,
    remind_interval_seconds: int | None,
    priority: str | None,
) -> tuple[builtins.list[str], builtins.list[object], dict[str, object], bool, builtins.list[str]]:
    """Build the SET clauses, params, event-log payload, owner-changed flag, and
    the human-readable change summary for update(). Collects only fields that
    were explicitly passed (None = no change), validating the interval and
    priority rungs. Returns (sets, params, payload, owner_changing, changes)."""
    sets: builtins.list[str] = []
    params: builtins.list[object] = []
    payload: dict[str, object] = {"task_id": task_id}
    # Human-readable summary of the changed fields, in the order they are
    # applied below — carried into the owner notification so a non-owner write
    # reports exactly what it changed.
    changes: builtins.list[str] = []
    # The four scalar fields map to a SET clause + a payload key; the payload
    # carries the new value for status/title and a replaced-flag for
    # description/results.
    for column, value, payload_key, payload_value, change_text in (
        ("status", status, "status", status, f"status → {status}"),
        ("title", title, "title", title, f"title → {title}"),
        ("description", description, "description_replaced", True, "description replaced"),
        ("results", results, "results_replaced", True, "results replaced"),
    ):
        if value is None:
            continue
        sets.append(f"{column} = %s")
        params.append(value)
        payload[payload_key] = payload_value
        changes.append(change_text)

    owner_changing = _owner_is_changing(owner)
    if owner_changing:
        sets.append("owner = %s")
        params.append(owner)
        changes.append(f"owner → #{owner}")
    if remind_interval_seconds is not None and remind_interval_seconds is not _UNSET:
        _validate_remind_interval_seconds(remind_interval_seconds)
        sets.append("remind_interval_seconds = %s")
        params.append(remind_interval_seconds)
        changes.append(f"remind_interval_seconds → {remind_interval_seconds}")
    if priority is not None:
        validate_priority(priority)
        sets.append("priority = %s")
        params.append(priority)
        payload["priority"] = priority
        changes.append(f"priority → {priority}")
    return sets, params, payload, owner_changing, changes


def _write_task_update(
    cur: psycopg.Cursor,
    task_id: int,
    status: str | None,
    sets: builtins.list[str],
    params: builtins.list[object],
    payload: dict[str, object],
    changes: builtins.list[str],
    title: str | None,
    note: str | None,
    owner: int | None,
    owner_changing: bool,  # noqa: FBT001 — internal helper flag, always passed by name
    actor: int,
) -> tuple[int | None, str, int | None]:
    """Apply an update() row write inside the caller's transaction.

    Holds the row FOR UPDATE, enforces root immutability, parent-close, and
    title-uniqueness rules, writes the row + optional note, and records the
    event log. Returns (old_owner, current_title, new_owner) for the post-commit
    notification."""
    # FOR UPDATE holds the row across the read -> write so two concurrent
    # reassignments cannot both act on the same stale owner.
    cur.execute(
        "SELECT owner, title, is_root FROM agent_tasks WHERE id = %s FOR UPDATE",
        (task_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"task {task_id} does not exist")
    old_owner, current_title, is_root = row
    # The system root task is immutable: it is the anchor of the task tree
    # and the parent of the cluster's top-level tasks, so it can never be
    # reassigned, completed, cancelled, or otherwise edited. Fail fast rather
    # than silently write.
    if is_root:
        raise ValueError(
            f"task {task_id} is the system root task and is immutable — "
            f"it cannot be reassigned, completed, cancelled, or otherwise edited"
        )
    if status in ("done", "cancelled"):
        cur.execute(
            "SELECT id, count(*) OVER () FROM agent_tasks "
            "WHERE parent_id = %s AND status = 'in_progress' "
            "ORDER BY id LIMIT 1",
            (task_id,),
        )
        active_child = cur.fetchone()
        if active_child is not None:
            child_id, child_count = active_child
            raise ValueError(
                f"task {task_id} has {child_count} in_progress child tasks "
                f"(e.g. #{child_id}) — close or cancel them first"
            )
    # A rename must keep create()'s invariant: no two in_progress
    # tasks share a title.
    if title is not None:
        cur.execute(
            "SELECT id, status FROM agent_tasks WHERE title = %s AND status = 'in_progress' AND id != %s LIMIT 1",
            (title, task_id),
        )
        dup = cur.fetchone()
        if dup is not None:
            raise ValueError(
                f"task with title {title!r} already exists (task #{dup[0]} is {dup[1]}) — "
                f"duplicate in_progress titles are not allowed"
            )
    sql = f"UPDATE agent_tasks SET {', '.join(sets)}, updated_at = now() WHERE id = %s"  # noqa: S608
    # type: ignore[arg-type] — the SET clause is assembled at runtime from
    # user-passed field names; psycopg accepts any str query.
    try:
        cur.execute(sql, (*params, task_id))  # type: ignore[arg-type]
    except psycopg.errors.UniqueViolation as exc:
        # Same race as create(): a concurrent rename landed this title between
        # the pre-check above and the UPDATE, and the partial unique index
        # rejected the write. Transaction aborted — generic message, see
        # _insert_task.
        raise ValueError(
            f"task with title {title!r} already exists among in_progress tasks — "
            "duplicate in_progress titles are not allowed (enforced by the database)"
        ) from exc

    # Append a timestamped note to results — works with any status or
    # standalone (no status change) as a drop-in for log().
    if note is not None:
        _append_note_to_results(cur, task_id, note)
        changes.append("note appended")

    new_owner = _owner_change_payload(payload, owner, old_owner, owner_changing)
    _log_task_update(actor, payload, owner_changing, new_owner)
    return old_owner, current_title, new_owner


def _owner_change_payload(
    payload: dict[str, object],
    owner: int | None,
    old_owner: int | None,
    owner_changing: bool,  # noqa: FBT001 — internal helper flag, always passed by name
) -> int | None:
    """Record a reassignment in the event payload; return the new owner (the
    old one when nothing changed)."""
    if owner_changing:
        payload["old_owner"] = old_owner
        payload["new_owner"] = owner
        return owner
    return old_owner


def _log_task_update(
    actor: int,
    payload: dict[str, object],
    owner_changing: bool,  # noqa: FBT001 — internal helper flag, always passed by name
    new_owner: int | None,
) -> None:
    """Record a task_update audit event (category=audit, kind=task_update)."""
    insert_event_log(
        event_type="task_update",
        agent_id=actor,
        source="self",
        target_agent_id=new_owner if owner_changing else None,
        payload=payload,
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
    content: str | None = None,
    note: str | None = None,
) -> None:
    """Any write resets the reminder clock. When the owner changes, both the old
    and new owner are notified (the new owner's message also carries a summary
    of the other fields changed in the same call). When the updater is not the
    owner, the owner is notified of the change and its author — except a
    parent-only reparent (no other field, no note), which is structural tree
    maintenance and stays silent. A terminated owner is never resurrected for
    a notification.

    Args:
        status: one of "in_progress", "done", "cancelled". Closing a
            task is rejected while any direct child is in progress.
        results: replaces the whole field; use note to append instead.
        owner: agent id to reassign to. None means no change — a task always
            has an owner.
        remind_interval_seconds: None means no change; reminders cannot be disabled.
            Positive seconds, capped at 24h.
        parent_id: reparent (explicit None = system root; int = set parent).
        content: deprecated alias for results.
    """
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
    content = coerce_str(content, "content", allow_none=True)
    note = coerce_str(note, "note", allow_none=True)
    status, results = _resolve_update_args(status, content, results)

    sets, params, payload, owner_changing, changes = _collect_update_fields(
        task_id, status, title, description, results, owner, remind_interval_seconds, priority
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
    # from this update, so any previous reminder is stale.
    sets.append("last_reminded_at = NULL")
    sets.append("reminder_count = 0")

    actor = ava._boot.agent_id()
    with ava.DB.transaction(), ava.DB.cursor() as cur:
        if parent_id is not _UNSET:
            sets.append("parent_id = %s")
            params.append(resolve_reparent(cur, task_id, parent_id))
        old_owner, current_title, new_owner = _write_task_update(
            cur,
            task_id,
            status,
            sets,
            params,
            payload,
            changes,
            title,
            note,
            owner,
            owner_changing,
            actor,
        )
        if parent_id is not _UNSET:
            changes.append("parent → root" if parent_id is None else f"parent → #{parent_id}")

    # Side effects run after the row change commits: telling an agent auto-wakes
    # it, so keep it out of the transaction. A reassignment carries the change
    # summary to the new owner; any other non-owner write notifies the
    # (unchanged) owner of what changed and by whom — so a task cancelled
    # behind its owner's back never goes unnoticed.
    _notify_after_update(
        task_id,
        title,
        current_title,
        old_owner,
        new_owner,
        owner_changing,
        actor,
        changes,
        parent_only,
    )

    # Live-refresh every open task board (fleet-wide invalidate + refetch).
    publish_task_updated_sync(actor, task_id)


def _should_notify_previous_owner(old_owner: int | None, actor: int) -> TypeGuard[int]:
    """True when the previous owner should be told the task left it: a
    terminated former owner is left asleep rather than resurrected just to be
    told (see _notify_owner_change), and the acting agent is never notified
    about its own action."""
    return old_owner is not None and old_owner != actor and not _is_terminated(old_owner)


def _notify_after_update(
    task_id: int,
    title: str | None,
    current_title: str,
    old_owner: int | None,
    new_owner: int | None,
    owner_changing: bool,  # noqa: FBT001 — internal helper flag, always passed by name
    actor: int,
    changes: builtins.list[str],
    parent_only: bool,  # noqa: FBT001 — internal helper flag, always passed by name
) -> None:
    """Post-commit owner notifications for update(): tell the new owner about
    the reassignment (with the change summary), or tell the unchanged owner
    that another agent wrote to its task.

    A parent-only reparent is skipped entirely — it is structural tree
    maintenance (cleanup / hierarchy moves), not a change to the task's own
    work, so no owner is woken for it (the 2026-08-27 incident: one batch
    reparent auto-resurrected 62 terminated owners through this path)."""
    if parent_only:
        return
    resolved_title = title if title is not None else current_title
    if _owner_actually_changed(owner_changing, old_owner, new_owner):
        _notify_owner_change(task_id, resolved_title, old_owner, new_owner, actor, changes=changes)
    elif new_owner is not None and actor != new_owner:
        _notify_owner_updated(task_id, resolved_title, new_owner, actor, changes)


def _notify_owner_change(
    task_id: int,
    title: str,
    old_owner: int | None,
    new_owner: int | None,
    actor: int,
    description: str | None = None,
    changes: builtins.list[str] | None = None,
) -> None:
    """Tell the new owner the task is theirs and the previous owner it is not,
    never notifying the caller (`actor`) about its own action.

    The new owner is always told, even when terminated: the system-note
    delivery auto-resurrects a terminated target (an assignment is a delegator
    direction, not a plain notification), so an assigned task never strands on
    a dead agent. The previous owner is the one deliberate skip -- waking a
    terminated former owner just to say the task left it is wasteful, and it
    never asked to be resurrected.

    When `description` is provided (from create()), it is appended to the new
    owner's message so they know what the task is about without a separate lookup.
    `changes` (from update()) is likewise appended, so a reassignment that also
    edits the task reports the other fields in the same message.

    Delivery is a system note (NoteTag `task`), not a peer chat: the timeline
    renders it as a system marker without an Agent prefix or peer timestamp
    (user ruling 2026-08-27).
    """
    if new_owner is not None and new_owner != actor:
        new_msg = f'Task #{task_id} "{title}" is now assigned to you (by agent #{actor}).'
        if changes:
            new_msg += "\n\n" + "\n".join(f"- {c}" for c in changes)
        if description:
            new_msg += f"\n\n{description}"
        # A task assignment is a delegator direction: the new owner is always
        # told, even when terminated — the system-note delivery auto-resurrects
        # so an assigned task never strands on a dead agent.
        ava.agents.send_system_note(new_owner, new_msg, resurrect=True)
    if _should_notify_previous_owner(old_owner, actor):
        ava.agents.send_system_note(
            old_owner,
            f'Task #{task_id} "{title}" you owned is no longer assigned to you.',
            resurrect=False,
        )


def _notify_owner_updated(
    task_id: int,
    title: str,
    owner: int,
    actor: int,
    changes: builtins.list[str],
) -> None:
    """Tell the task's owner that another agent changed their task.

    Fires on any non-owner business write (status, title, description,
    results, note, priority, ...) so an owner is never left unaware that its
    task was touched -- the case that motivated it: task #494 was cancelled by
    another agent and its owner only found out later. A terminated owner is
    deliberately left asleep: this is a notification, not a delegator
    direction, so it must not auto-resurrect the owner just to be told (user
    ruling 2026-08-27 -- notification messages never resurrect a terminated
    owner; only real delegator/user business messages may). Delivered as a
    system note with resurrect=False, so the delivery path itself enforces
    the ruling. `actor` is never notified about its own action -- update()
    only calls this when `actor != owner`.
    """
    if _is_terminated(owner):
        return
    detail = "\n".join(f"- {c}" for c in changes)
    ava.agents.send_system_note(
        owner,
        f'Task #{task_id} "{title}" was updated by agent #{actor}:\n{detail}',
        resurrect=False,
    )


def _is_terminated(agent_id: int) -> bool:
    """True when an agent is gone -- terminated, or absent from agents_meta.

    Gates only the previous-owner notification: a terminated former owner is
    left asleep rather than resurrected just to be told a task left it."""
    with ava.DB.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        meta = cur.fetchone()
    return meta is None or meta[0] == "terminated"


def log(task_id: int, message: str) -> None:
    """Append a timestamped line to a task's result log."""
    task_id = coerce_typed(task_id, "task_id", int)
    message = coerce_str(message, "message")
    update(task_id, note=message)


def get(task_id: int) -> Task:
    """Return the task with this id."""
    task_id = coerce_typed(task_id, "task_id", int)
    with ava.DB.cursor() as cur:
        cur.execute(f"SELECT {_COLS} FROM agent_tasks WHERE id = %s", (task_id,))  # noqa: S608
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"task {task_id} does not exist")
    return _row_to_task(row)


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
            f") SELECT {_COLS} FROM subtree{_where_clause(filters)} ORDER BY created_at, id"
        )
        return sql, [parent, *params]

    if parent is not None:
        filters.append("parent_id = %s")
        params.append(parent)
    sql = f"SELECT {_COLS} FROM agent_tasks{_where_clause(filters)} ORDER BY created_at, id"  # noqa: S608
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
    if status is not None and status not in _STATUSES and status != "ongoing":
        raise ValueError(f"status must be one of {sorted(_STATUSES)} or 'ongoing', got {status!r}")

    sql, params = _build_list_query(parent, owner, status, recursive)
    with ava.DB.cursor() as cur:
        cur.execute(sql, params)
        return [_row_to_task(r) for r in cur.fetchall()]
