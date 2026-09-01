---
type: doc
title: Task Model — Root Task
description: "The system-level root task: special properties, immutability enforcement, top-level-task anchor behavior, and the is_root database marker"
tags:
- fleet
- tasks
- data-model
- root-task
---

# Root Task

The baseline in `db/schema.sql` seeds a system-level **root task** (`agent_tasks.is_root = TRUE`). Each deployment database always has exactly one (`WHERE NOT EXISTS` idempotent, repeated bootstrap has no side effects). The `is_root` column is a database-side system marker — not in the `Task` data class.

## Special Properties

- **`owner IS NULL`** — the **only** task without an owner; `created_by='system'`, `status` is always `ongoing` (its permanent state, pinned by the `agent_tasks_root_status_ongoing` DB CHECK). This is why `Task.owner` and `Task.remind_interval_seconds` are `int | None`: reading the root task returns `None` for both.
- **Never reminded** — task-maintenance's remind/escalate SQL all include `NOT is_root`.
- **Not filtered by `get()`/`list()`** — they return the root task. Callers reading `owner` must tolerate `None` (the frontend displays root as " · unowned").

## Immutability (Enforced)

`update()` (SDK) and gateway `PATCH /api/tasks/{id}` both check `is_root` before writing and reject immediately: SDK raises `ValueError`, PATCH returns `422`. The root task cannot have its status, owner, description, or reminder interval changed.

## Parent of Top-Level Tasks Only

`create()`'s `parent` parameter is **required** (no default): a cluster top-level task passes the root's id (1) explicitly, a subtask passes an existing task's id. The root is therefore the parent of the cluster's top-level tasks only, and the entire registry is a tree rooted at root, with no orphan top-level nodes. `parent_id IS NULL` **only** appears on the root itself. Legacy databases were backfilled via the `root-task-default-parent` migration (changing all existing `parent_id IS NULL AND NOT is_root` to point to root).

## Summary

`is_root` serves three purposes: **top-level-task anchor + immutability enforcement + reminder exclusion**.
