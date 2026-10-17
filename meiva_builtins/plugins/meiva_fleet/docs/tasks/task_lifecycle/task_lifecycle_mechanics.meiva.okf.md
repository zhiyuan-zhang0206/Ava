---
type: doc
title: Task Lifecycle — State, Concurrency & Event Log
description: "Task lifecycle state machine, SELECT FOR UPDATE concurrency safety, and the task audit event log"
tags:
- fleet
- tasks
- lifecycle
- concurrency
---

# Task Lifecycle — State, Concurrency & Event Log

## State Transitions

```
in_progress ──→ done
     │
     └──→ cancelled
```

- **`in_progress`**: created and actively worked (a task is born `in_progress` — user ruling 2026-08-29 dropped the meaningless `open` state); **`done`**: completed (outcome in `results`); **`cancelled`**: from any state.
- The system root task is pinned to `in_progress` permanently and is immutable — the root is not a status of its own (`"ongoing"` was removed by user ruling 2026-09-15).

`status` is enforced by a database `CHECK` constraint; passing an illegal value raises `ValueError`.

## Concurrency Safety

`update`/`log` (log delegates to update) lock the target row with `SELECT ... FOR UPDATE` in a transaction: no lost owner transfers, no lost appended lines.

```sql
BEGIN;
SELECT owner, title FROM agent_tasks WHERE id = %s FOR UPDATE;  -- row lock
UPDATE agent_tasks SET owner = %s, updated_at = now() WHERE id = %s;
COMMIT;
```

Notifications (`send_system_note`) are executed **after** the transaction commits—avoiding waking other agents while holding the lock.

## Event Log

Every `create`, `update`, and `log` records an audit fact in `audit_events` in the same transaction (`record_audit`), recording `agent_id` (the actor), `payload` (the changes), and `target_agent_id` (new owner on owner changes), used for auditing and fleet view replay.
