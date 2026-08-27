---
type: doc
title: Task Model — Task Data Model
description: "Task data class overview with field-by-field, reminder, root task, and database mapping references"
tags:
- fleet
- tasks
- data-model
---

# Task Model

The `Task` data class represents one work item in the task registry. All tasks form a parent-child tree rooted at the system [[ava_builtins/plugins/ava_fleet/tasks/task_model/task_model_root.ava.okf.md|Root Task]].

```python
@dataclass
class Task:
    id: int                    # auto-increment primary key, globally unique
    parent_id: int | None      # parent task id, NULL only on root task (others default to root)
    title: str                 # short title; unique among open/in_progress; can be renamed
    description: str           # full description — the first thing an assignee should read
    results: str | None        # result log — appended via log/note or replaced via update
    status: str                # "open" | "in_progress" | "done" | "cancelled" ("ongoing" = root-only permanent state)
    owner: int | None          # responsible agent id; NULL only on root task
    created_by: str            # creator agent's id string ("system" for root)
    created_at: datetime       # creation time
    updated_at: datetime       # last modification time
    remind_interval_seconds: int | None = None      # seconds without update after which to remind; cannot be turned off, NULL only on root
    last_reminded_at: datetime | None = None   # last reminder time; cleared on any write
    reminder_count: int = 0                     # reminder count; cleared on any write
    priority: str = "P2"                     # "P0".."P3"; added in #663
```

> The `is_root` column (`agent_tasks.is_root BOOLEAN`) is a database-side system marker — not in the `Task` data class.

## Design Summary

- **Always parented**: every task has a parent — explicit or the root; `parent_id IS NULL` only on root.
- **Always owned**: every task has an owner except root; ownership transfers between agents, never released.
- **Always reminded**: reminders cannot be turned off (default 30 min, max 24h); any write resets the window.
- **Root immutable**: the root task rejects all mutations (SDK `ValueError`, API `422`).

## Reference

- [[ava_builtins/plugins/ava_fleet/tasks/task_model/task_model_fields.ava.okf.md|Field Details]] — `id`, `parent_id`, `title`/`description`, `results`, `status`, `owner`, `created_by`, timestamps, `priority`
- [[ava_builtins/plugins/ava_fleet/tasks/task_model/task_model_reminders.ava.okf.md|Reminder Mechanism]] — `remind_interval_seconds`, `last_reminded_at`, `reminder_count`, daemon backoff & escalation
- [[ava_builtins/plugins/ava_fleet/tasks/task_model/task_model_root.ava.okf.md|Root Task]] — system root, immutability, top-level-task anchor
- [[ava_builtins/plugins/ava_fleet/tasks/task_model/task_model_database.ava.okf.md|Database Mapping & Task Tree]] — `agent_tasks` table, `_COLS`, recursive subtree queries

See also: [[task_lifecycle.ava.okf.md|Task Lifecycle]], [[task_notification.ava.okf.md|Task Notification]].
