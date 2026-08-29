---
type: doc
title: Task Model — Field Details
description: "Field-by-field reference for the Task data class: id, parent_id, title, description, results, status, owner, created_by, timestamps, priority"
tags:
- fleet
- tasks
- data-model
---

# Task Model — Field Details

## `id`

Auto-increment primary key, globally unique. Assigned by the database on creation.

## `parent_id`

Pointer to parent task (foreign key). `NULL` **only** appears on the system [[task_model_root.ava.okf.md|Root Task]] — all other tasks always have a parent: if `create()` omits `parent`, it falls back to root. All tasks form a parent-child tree; query the full subtree with `list(parent=..., recursive=True)` via recursive CTE.

Constraint: a child is always created after its parent (`created_at` monotonically increasing) — cycles are impossible.

## `title` / `description`

- `title`: Short one-line name shown in `list` results and notifications. Renamable via `update(title=...)`; must be unique among `in_progress` tasks (same check as `create`).
- `description`: Full description — what and why, the first read for the assignee. Set at `create`, revisable via `update(description=...)`.

Old field name `brief` is a deprecated alias (Task attribute + `create` parameter), to be removed.

## `results`

Result log. Append progress with `log(task_id, message)` — adds a timestamped line; `update(results=...)` **replaces the entire field**. Initially `NULL`.

Old field name `content` is a deprecated alias (Task attribute + `update` parameter), to be removed.

## `status`

One of three values an agent can assign (`CHECK` constraint): `"in_progress"`, `"done"`, `"cancelled"`. A task is born `"in_progress"` (DB default — the `"open"` state was dropped by user ruling 2026-08-29: creation starts the work immediately). The `CHECK` also admits `"ongoing"` — the system root task's permanent state, never assignable via `create()`/`update()`/PATCH and pinned by the `agent_tasks_root_status_ongoing` DB constraint. See [[task_lifecycle.ava.okf.md|Task Lifecycle]].

## `owner`

The `id` of the currently responsible agent. **Except for the system root task, every task always has an owner** — `create()` defaults owner to the creator; `update(owner=None)` means "no change" (does not release). Ownership is only transferred between agents. The sole exception is the [[task_model_root.ava.okf.md|Root Task]] (`owner IS NULL`): `get()`/`list()` may return it, so callers reading `owner` must tolerate `None`.

By convention, claiming a task sets `owner=self, status="in_progress"`. Changing owner triggers a notification (see [[task_notification.ava.okf.md|Task Notification]]).

## `created_by`

The id string of the agent who created this task (`"system"` for root; historical rows may contain `"user"`).

## `created_at` / `updated_at`

Timestamps auto-managed by the database. `updated_at` is refreshed on every `update`/`log` call.

## `priority`

One of `"P0"` (highest) through `"P3"` (lowest), default `"P2"` (`CHECK` constraint, same four levels as notice). `create()`/`update()` validate and raise `ValueError` for illegal values; `update(priority=None)` means "no change". The task board sorts by priority within the same status column (display only, the frontend doesn't write it). When a task escalates to the human queue due to being overdue, the `agent_notices` entry inherits the task's `priority` (see [[task_model_reminders.ava.okf.md|Reminders]]).
