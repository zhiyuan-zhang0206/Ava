"""Validation and database-write helpers for fleet task updates."""

from __future__ import annotations

import builtins
import math

import psycopg

from shared.audit_events import insert_event_log
from shared.priority import DEFAULT_REMIND_INTERVAL_SECONDS, Priority, validate_priority
from shared.task_notes import task_note_line

# The statuses update() may assign to a regular task. 'ongoing' marks
# long-running active work, so it is exempt from reminder scans that only read
# in_progress rows. The root remains permanently ongoing and immutable (see
# _write_task_update); create() still begins every regular task in_progress.
_STATUSES = frozenset({"in_progress", "ongoing", "done", "cancelled"})

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
    # The update also bumps updated_at: a note append is real task activity,
    # so the task graph's last-activity window must see it (Task #1969).
    cur.execute(
        "UPDATE agent_tasks SET results = %s, updated_at = now() WHERE id = %s",
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


def _validate_budgets(
    token_budget: int | None, usd_budget: float | int | None
) -> tuple[int | None, float | None]:
    """Validate optional task ceilings and normalize the USD value to float."""
    if isinstance(token_budget, bool):
        raise TypeError("token_budget must be int or None, got bool")
    if isinstance(usd_budget, bool):
        raise TypeError("usd_budget must be int, float, or None, got bool")
    if token_budget is not None and token_budget <= 0:
        raise ValueError(f"token_budget must be a positive integer, got {token_budget!r}")
    if usd_budget is None:
        return token_budget, None
    normalized_usd = float(usd_budget)
    if not math.isfinite(normalized_usd) or normalized_usd <= 0:
        raise ValueError(f"usd_budget must be a positive finite number, got {usd_budget!r}")
    return token_budget, normalized_usd


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
    actor: int | None,
) -> tuple[int | None, str, int | None]:
    """Apply an update() row write inside the caller's transaction.

    Holds the row FOR UPDATE, enforces root immutability, ongoing ownership,
    parent-close, and title-uniqueness rules, writes the row + optional note,
    and records the event log. Returns (old_owner, current_title, new_owner)
    for the post-commit notification."""
    # FOR UPDATE holds the row across the read -> write so two concurrent
    # reassignments cannot both act on the same stale owner.
    cur.execute(
        "SELECT owner, title, is_root, status FROM agent_tasks WHERE id = %s FOR UPDATE",
        (task_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"task {task_id} does not exist")
    old_owner, current_title, is_root, current_status = row
    # The system root task is immutable: it is the anchor of the task tree
    # and the parent of the cluster's top-level tasks, so it can never be
    # reassigned, completed, cancelled, or otherwise edited. Fail fast rather
    # than silently write.
    if is_root:
        raise ValueError(
            f"task {task_id} is the system root task and is immutable — "
            f"it cannot be reassigned, completed, cancelled, or otherwise edited"
        )
    # A process identity is the same value agents read as ava.self.AGENT_ID.
    # System tooling runs without one and deliberately remains outside this
    # agent-to-agent ownership gate.
    if (
        status == "ongoing"
        and status != current_status
        and actor is not None
        and actor != old_owner
    ):
        cur.execute(
            "WITH RECURSIVE owner_lineage(id, spawner) AS ("
            "SELECT id, spawner FROM agents_meta WHERE id = %s "
            "UNION "
            "SELECT parent.id, parent.spawner FROM agents_meta parent "
            "JOIN owner_lineage child ON child.spawner = 'agent:' || parent.id::TEXT"
            ") SELECT 1 FROM owner_lineage WHERE id = %s LIMIT 1",
            (old_owner, actor),
        )
        if cur.fetchone() is None:
            raise ValueError("only the owner or a delegator can set a task to ongoing")
    if status in ("done", "cancelled"):
        cur.execute(
            "SELECT id, count(*) OVER () FROM agent_tasks "
            "WHERE parent_id = %s AND status IN ('in_progress', 'ongoing') "
            "ORDER BY id LIMIT 1",
            (task_id,),
        )
        active_child = cur.fetchone()
        if active_child is not None:
            child_id, child_count = active_child
            raise ValueError(
                f"task {task_id} has {child_count} active child tasks "
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
    actor: int | None,
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
