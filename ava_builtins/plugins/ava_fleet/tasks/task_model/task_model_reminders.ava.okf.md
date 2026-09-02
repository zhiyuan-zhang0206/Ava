---
type: doc
title: Task Model — Reminder Mechanism
description: "Reminder fields and daemon behavior: remind_interval_seconds, last_reminded_at, reminder_count, backoff, and escalation"
tags:
- fleet
- tasks
- data-model
- reminders
---

# Task Model — Reminder Mechanism

## Fields

- `remind_interval_seconds: int | None` — seconds without an update after which to remind the owner. Default 1800 (30 minutes). **Reminders cannot be turned off**: passing `None` to `create` falls back to default, passing `None` to `update` means "no change". Maximum 24h (86400s). Can be `NULL` only on the [[task_model_root.ava.okf.md|Root Task]] (which is never reminded).
- `last_reminded_at: datetime | None` — last reminder time; cleared on any write operation.
- `reminder_count: int` — number of reminders sent; cleared on any write operation.

Any `update`/`log` write clears both `last_reminded_at` and `reminder_count`, restarting the overdue window from the latest update.

## Daemon Behavior

The task-maintenance daemon only sends reminders for tasks where **`status='in_progress'` AND `owner IS NOT NULL` AND `NOT is_root`** (ongoing, done, and cancelled tasks are not reminded; the root task never gets reminded). Post-2026-08-29 every task is born `in_progress`, so a newly created task is reminder-eligible immediately.

Within the same overdue window, at most one reminder per backoff period (`AVA_TASK_REMINDER_BACKOFF_SECONDS`, default 3600s). When `reminder_count` reaches `AVA_TASK_ESCALATE_N` (default 3), escalation occurs:

- If the parent has an owner → notify the parent owner.
- If the parent has no owner (a top-level task under the unowned system root) → insert a `require_response` notice into the human queue.

Details in [[ava_builtins/plugins/ava_fleet/task_maintenance/task-maintenance.ava.okf.md|Task-Maintenance]].
