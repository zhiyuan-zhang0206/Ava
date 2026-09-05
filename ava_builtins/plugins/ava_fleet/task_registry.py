"""Task tracking — shared, persistent work items. All agents are peers: any
agent can update any task; owners are reminded periodically; a task may have
a parent."""

from __future__ import annotations

import builtins
import math
from dataclasses import dataclass
from typing import TypeGuard

import psycopg

import ava
import ava._boot
import ava.agents
from ava._sdk_validation import coerce_str, coerce_typed
from shared.audit_events import insert_event_log
from shared.live_announce import publish_task_created_sync, publish_task_updated_sync
from shared.task_owner_notifications import owner_change_notifications
from shared.task_reparent import resolve_reparent
from shared.task_timestamps import render_task_timestamps

from ._task_update import (
    _DEFAULT_PRIORITY,
    _STATUSES,
    _UNSET,
    _collect_update_fields,
    _nothing_to_update,
    _owner_actually_changed,
    _resolve_create_args,
    _resolve_update_args,
    _validate_budgets,
    _write_task_update,
)
from ._task_update import (
    _MAX_REMIND_INTERVAL_SECONDS as _MAX_REMIND_INTERVAL_SECONDS,
)
from ._task_update import (
    _append_note_to_results as _append_note_to_results,
)
from ._task_update import (
    _log_task_update as _log_task_update,
)
from ._task_update import (
    _owner_change_payload as _owner_change_payload,
)
from ._task_update import (
    _owner_is_changing as _owner_is_changing,
)
from ._task_update import (
    _Unset as _Unset,
)
from ._task_update import (
    _validate_remind_interval_seconds as _validate_remind_interval_seconds,
)

# `list` / `get` shadow builtins intentionally: these are the agent-facing names
# (ava.tasks.list / ava.tasks.get), matching ava.shell.sessions.list. flake8-builtins
# (`A`) is not in this repo's ruff select, and the SDK renderer reads annotations
# as strings without evaluating them, so the shadow is runtime-cosmetic. It is
# NOT invisible to a type checker, though: within this module the name `list`
# resolves to the function, so annotations that mean the builtin container are
# spelled `builtins.list[...]`.
__all_for_ava__ = ["Task", "create", "create_and_assign", "get", "list", "log", "update"]

# Column order matches the Task field order and the Task(*row) unpacking in
# _row_to_task; keep the three aligned.
_COLS = "id, parent_id, title, description, results, status, owner, created_by, created_at, updated_at, remind_interval_seconds, last_reminded_at, reminder_count, priority, token_budget, usd_budget, token_used, usd_used"


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
    token_budget: int | None = None
    usd_budget: float | None = None
    token_used: int = 0
    usd_used: float = 0.0

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


@dataclass(frozen=True)
class TaskBudgetBreach:
    """One task ceiling crossed for the first time by tagged LLM usage."""

    task_id: int
    title: str
    owner: int | None
    budget_kind: str
    used: int | float
    budget: int | float


def _row_to_task(row: tuple) -> Task:
    """Build a Task from a _COLS row; timestamps rendered bare (issue #181)."""
    return Task(*render_task_timestamps(row, _COLS))


def _ensure_parent_exists(cur: psycopg.Cursor, parent: int) -> None:
    """Validate `parent` names an existing task; raise ValueError otherwise.

    Also rejects parent=1 on a deployment where task 1 is not the system root
    (a migrated database whose root carries another id): the documented root
    id (1) must not silently attach a top-level task under a different parent.
    A closed (done / cancelled) parent is rejected too: a closed task must
    never gain children -- tasks created after a parent closed are what
    produced the false-orphan rows in the task graph (task #1975). The system
    root is exempt by construction (its status is 'ongoing', never closed).
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
    if row[2] in ("done", "cancelled"):
        raise ValueError(
            f"parent task {parent} is {row[2]} — a closed task cannot be the "
            "parent of a new task; reopen it or pass the system root task id "
            "(1) for a top-level task"
        )


def _insert_task(
    cur: psycopg.Cursor,
    title: str,
    description: str,
    effective_parent: int | None,
    effective_owner: int,
    remind_interval_seconds: int,
    priority: str,
    token_budget: int | None,
    usd_budget: float | None,
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
    sql = f"INSERT INTO agent_tasks (parent_id, title, description, created_by, owner, remind_interval_seconds, priority, token_budget, usd_budget) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLS}"  # noqa: S608
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
                token_budget,
                usd_budget,
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
            "token_budget": token_budget,
            "usd_budget": usd_budget,
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
    token_budget: int | None = None,
    usd_budget: float | int | None = None,
    brief: str | None = None,
) -> Task:
    """
    Args:
        title: unique among in_progress tasks.
        parent: id of an existing task this task descends from. The system
            root task (id 1) parents only top-level tasks; every other task
            must reference an existing task. Raises ValueError when the
            parent does not exist, is closed (done / cancelled — a closed
            task never gains children), or, for parent=1, when task 1 is not
            the system root on this deployment.
        remind_interval_seconds: cannot be disabled; None means the priority
            default (P0 30m / P1 1h / P2 2h / P3 4h), capped at 24h; an
            explicit value wins over the default.
        owner: agent to assign to; None means you. An owner other than you is
            notified.
        priority: "P0" (highest) through "P3" (lowest).
        token_budget: optional positive ceiling for task-tagged LLM tokens.
        usd_budget: optional positive finite USD ceiling for task-tagged LLM cost.
            Untagged LLM calls do not count toward either ceiling.
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
    token_budget = coerce_typed(token_budget, "token_budget", int, allow_none=True)
    usd_budget = coerce_typed(usd_budget, "usd_budget", (int, float), allow_none=True)
    brief = coerce_str(brief, "brief", allow_none=True)
    description, remind_interval_seconds, priority = _resolve_create_args(
        brief, description, remind_interval_seconds, priority
    )
    token_budget, usd_budget = _validate_budgets(token_budget, usd_budget)
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
            token_budget,
            usd_budget,
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
    token_budget: int | None = None,
    usd_budget: float | int | None = None,
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
    token_budget = coerce_typed(token_budget, "token_budget", int, allow_none=True)
    usd_budget = coerce_typed(usd_budget, "usd_budget", (int, float), allow_none=True)
    token_budget, usd_budget = _validate_budgets(token_budget, usd_budget)
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
        token_budget=token_budget,
        usd_budget=usd_budget,
    )

    # 3. Return both so the caller can track the task and the agent.
    return task, agent_id


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
        status: one of "in_progress", "ongoing", "done", "cancelled".
            ongoing marks long-running active work. Closing a task is rejected
            while any direct child is in progress or ongoing. Only the owner or
            its delegator may change a task into ongoing; calls without an
            agent identity remain allowed for system tooling.
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

    # Agent-scoped side effects run after the row change commits: telling an
    # agent auto-wakes it, so keep it out of the transaction. System tooling
    # has no actor for a task note or TaskUpdated; like gateway PATCH, its
    # committed write relies on the board's normal poll.
    if actor is not None:  # pyright: ignore[reportUnnecessaryComparison] -- agent_id() is None before bootstrap.
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
    for note in owner_change_notifications(
        task_id,
        title,
        None,
        new_owner,
        actor=actor,
        previous_owner_terminated=False,
        description=description,
        changes=changes,
    ):
        ava.agents.send_system_note(
            note.agent_id,
            note.content,
            task_id=task_id if note.resurrect else None,
            resurrect=note.resurrect,
        )
    if _should_notify_previous_owner(old_owner, actor):
        for note in owner_change_notifications(
            task_id,
            title,
            old_owner,
            None,
            actor=actor,
            previous_owner_terminated=False,
        ):
            ava.agents.send_system_note(note.agent_id, note.content, resurrect=note.resurrect)


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
        task_id=task_id,
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


def record_task_usage(task_id: int, *, token_count: int, cost_usd: float) -> None:
    """Add one explicitly task-tagged LLM call and notify on a first breach.

    The row lock makes the cumulative totals and one-shot notification markers
    atomic across concurrent task turns. Calls without an explicit task id do
    not reach this function and are intentionally absent from every task total.
    """
    if token_count < 0:
        raise ValueError(f"token_count must be non-negative, got {token_count!r}")
    if not math.isfinite(cost_usd) or cost_usd < 0:
        raise ValueError(f"cost_usd must be a finite non-negative number, got {cost_usd!r}")
    with ava.DB.transaction(), ava.DB.cursor() as cur:
        cur.execute(
            "SELECT title, owner, token_budget, usd_budget, token_used, usd_used, "
            "token_budget_notified_at, usd_budget_notified_at "
            "FROM agent_tasks WHERE id = %s FOR UPDATE",
            (task_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"task {task_id} does not exist")
        (
            title,
            owner,
            token_budget,
            usd_budget,
            token_used,
            usd_used,
            token_notified,
            usd_notified,
        ) = row
        new_token_used = token_used + token_count
        new_usd_used = usd_used + cost_usd
        token_breached = (
            token_budget is not None and token_notified is None and new_token_used >= token_budget
        )
        usd_breached = (
            usd_budget is not None and usd_notified is None and new_usd_used >= usd_budget
        )
        cur.execute(
            "UPDATE agent_tasks SET token_used = %s, usd_used = %s, "
            "token_budget_notified_at = CASE WHEN %s THEN now() ELSE token_budget_notified_at END, "
            "usd_budget_notified_at = CASE WHEN %s THEN now() ELSE usd_budget_notified_at END "
            "WHERE id = %s",
            (new_token_used, new_usd_used, token_breached, usd_breached, task_id),
        )

    breaches: builtins.list[TaskBudgetBreach] = []
    if token_breached and token_budget is not None:
        breaches.append(
            TaskBudgetBreach(task_id, title, owner, "token", new_token_used, token_budget)
        )
    if usd_breached and usd_budget is not None:
        breaches.append(TaskBudgetBreach(task_id, title, owner, "USD", new_usd_used, usd_budget))
    for breach in breaches:
        if breach.owner is None:
            continue
        ava.agents.send_system_note(
            breach.owner,
            f'Task #{breach.task_id} "{breach.title}" exceeded its {breach.budget_kind} budget: '
            f"{breach.used} used of {breach.budget}. Finish the in-flight unit, update the task, "
            "and do not begin additional work without a new budget.",
            task_id=breach.task_id,
            resurrect=False,
        )


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
