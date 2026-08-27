---
type: doc
title: Task Lifecycle — Task Lifecycle & API
description: "Complete ava.tasks API: create / create_and_assign / get / list / update / log, state transitions, owner change conventions, reminder mechanism, and SELECT FOR UPDATE concurrency safety"
tags:
- fleet
- tasks
- api
- lifecycle
- concurrency
---

# Task Lifecycle — Task Lifecycle & API

## State Transitions

```
open ──→ in_progress ──→ done
  │           │
  └───────────┴──→ cancelled
```

- **`open`**: created (creator is default owner); **`in_progress`**: actively worked; **`done`**: completed (outcome in `results`); **`cancelled`**: from any state.

`status` is enforced by a database `CHECK` constraint; passing an illegal value raises `ValueError`.

## API

### `create(title, description, *, parent, owner=None, priority="P2", remind_interval_seconds=None) -> Task`

Create a task and return the full `Task`. `title` is a single line (unique among `open`/`in_progress`); `description` is the task description.

- **`parent` is required** — the id of an existing task this task descends from. The system root task (**id 1**) is the parent of the cluster's top-level tasks **only**; a subtask must pass the id of an existing task as its parent (create the parent first). A missing parent id raises `ValueError`; `parent=1` is also rejected when task 1 is not the system root on that deployment (so the documented root id can never silently attach a task under a different parent). `create_and_assign` validates the parent before spawning, so a bad parent never leaves an orphaned agent behind.
- **Creator defaults to owner** (`owner=None`); when explicitly passing another agent id, the task is directly assigned to that agent, and that agent receives a notification including title + description.
- `priority`: `"P0"` (highest)..`"P3"` (lowest), default `"P2"`; illegal value raises `ValueError` (#663; board sorts within a status column by this).
- `remind_interval_seconds`: no-update duration after which the owner is reminded. Default scales with priority — P0 30m / P1 1h / P2 2h / P3 4h. **Cannot be disabled**—`None` falls back to the priority default; an explicit value wins; cap 24h; out-of-range raises `ValueError`.
- **Rejects duplicate titles** among `open`/`in_progress` tasks (`ValueError`).
- Triggers a `task_create` event log + publishes `task_created` (SSE, board invalidates and refetches).

### `create_and_assign(title, description, *, preset="coder", label=None, config_overlay=None, parent, priority="P2", remind_interval_seconds=None) -> (Task, int)`

Spawn an agent and create a task assigned to it in one call: spawns per `preset`/`config_overlay` (the agent must exist to be an owner), then `create(owner=that agent)`—the create notification already carries task id + title + description, no separate `send_message` needed. Returns `(task, agent_id)`.

### `get(task_id) -> Task`

Return the task by id; raises `ValueError` if it does not exist. Read `description` before working; read `results` before reporting.

### `list(*, parent=None, owner=None, status=None, recursive=False) -> list[Task]`

Filter the task list, ordered by `created_at` ascending. All parameters are optional and combinable.

| Parameter | Effect |
|-----------|--------|
| `owner=your_id` | Your tasks |
| `owner=None` (default) | Don't filter by owner (return all owners) |
| `status="open"` | Only open tasks |
| `parent=some_id` | Direct child tasks |
| `parent=some_id, recursive=True` | Entire subtree (recursive CTE) |
| No parameters | All tasks |

### `update(task_id, *, status=None, title=None, description=None, results=None, owner=<unchanged>, remind_interval_seconds=<unchanged>, priority=None, note=None) -> None`

Modify task fields, **pass only what you want to change**—omitted fields remain unchanged. `results` is replaced wholesale; to append progress, use `note=` or `log()`.

- `title`: rename; must be unique among `open`/`in_progress` tasks, conflict raises `ValueError` (same duplicate check as `create`, checked inside row lock).
- `status`: changing to `done` or `cancelled` is rejected while any direct child remains `open` or `in_progress`; close or cancel those children first. Other status changes and non-status updates are unaffected.
- `owner`: pass an agent id to transfer/claim; **not passing or passing `None` both mean "unchanged"**—a task always has an owner, `owner=None` no longer releases a task.
- `remind_interval_seconds`: **not passing or passing `None` both mean "unchanged"**—reminders cannot be disabled; only passing a positive integer (≤24h) changes it, exceeding raises `ValueError`.
- `priority`: `None` (default) means "unchanged"; passing `"P0"`..`"P3"` writes it, illegal value raises `ValueError` (#663).
- `note`: appends a line `[YYYY-MM-DD HH:MM:SS] <note>` to `results`, can be used alongside any status change or alone as a substitute for `log()`. The stamp is built by `shared.task_notes.task_note_line` — the same agent-facing representation and the same timezone as every other timestamp an agent reads, shared with the gateway's drain writer so the two cannot drift.
- Any write resets the reminder counter (`last_reminded_at` / `reminder_count` zeroed).
- Each successful write publishes `task_updated` (SSE, board invalidates and refetches).
- Old parameter names `content` (= `results`), `create`'s `brief` (= `description`) are kept as deprecated aliases.

### `log(task_id, message) -> None`

Append a line `[YYYY-MM-DD HH:MM:SS] message` to `results`—**delegates to `update(task_id, note=message)`**, same timestamped append, same reminder counter reset.

**Common operation patterns**:

```python
ava.tasks.update(task_id, owner=ava.self.AGENT_ID, status="in_progress")  # claim and start
ava.tasks.log(task_id, "Finished phase one")                               # log progress
ava.tasks.update(task_id, status="done")                                    # complete
ava.tasks.update(task_id, owner=other_agent_id)                             # transfer to another agent
```

## Concurrency Safety

`update`/`log` (log delegates to update) lock the target row with `SELECT ... FOR UPDATE` in a transaction: no lost owner transfers, no lost appended lines.

```sql
BEGIN;
SELECT owner, title FROM agent_tasks WHERE id = %s FOR UPDATE;  -- row lock
UPDATE agent_tasks SET owner = %s, updated_at = now() WHERE id = %s;
COMMIT;
```

Notifications (`send_message`) are executed **after** the transaction commits—avoiding waking other agents while holding the lock.

## Event Log

Every `create`, `update`, and `log` writes a structured event log via `insert_event_log`, recording `agent_id` (the actor), `payload` (the changes), and `target_agent_id` (new owner on owner changes), used for auditing and fleet view replay.
