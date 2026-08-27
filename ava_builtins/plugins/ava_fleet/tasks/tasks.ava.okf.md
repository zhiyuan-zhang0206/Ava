---
type: doc
title: Tasks — Shared Task Registry Overview
description: Entry index for the ava.tasks subsystem — navigation document for Task data model, lifecycle API, state transitions and notification mechanism
tags:
- fleet
- tasks
- coordination
- overview
---

# Tasks — Shared Task Registry Overview

## What it is

`ava.tasks` is a shared persistent task system. A task outlives the agent working on it: a task carries **what to do** (description), **how it's going** (status), **result records** (results), and **who is currently responsible** (owner). Any agent can claim or reassign tasks — there is no fixed hierarchy, only who is currently processing it. Except for the system root task, every task always has an owner, no unclaimed state exists; `get()`/`list()` may return that root task with `owner=None` (see [[task_model.ava.okf.md|Task Model]] "Root Task" section).

## API Overview

```python
task = ava.tasks.create(title, description, parent=root_id, owner=None, priority="P2")  # Create; parent is required (root id 1 for top-level tasks)
task, aid = ava.tasks.create_and_assign(title, description, parent=root_id)   # Create + spawn agent to claim
task = ava.tasks.get(task_id)                                 # Read by id
tasks = ava.tasks.list(owner=..., status=...)                 # Filter list
ava.tasks.update(task_id, status=..., owner=..., priority=..., note=...)  # Change status/results/owner/priority + append progress
ava.tasks.log(task_id, "note")                                # Append a timestamped line (= update(note=))
```

## Data Model at a Glance

```
agent_tasks (table)
├── id, parent_id          — Primary key and tree structure
├── title, description, results — Task description and result log
├── status                 — CHECK IN ('open','in_progress','done','cancelled')
├── owner                  — REFERENCES agents(id) (always has an owner except root task)
├── priority                — TEXT NOT NULL DEFAULT 'P2', CHECK IN ('P0','P1','P2','P3') (added in #663)
├── is_root                — BOOLEAN, system root task marker (the only row with owner=NULL)
├── created_by             — TEXT (creator agent id string; 'system' for root)
├── remind_interval_seconds        — Reminder interval in seconds (cannot be disabled, NULL only appears on root task)
├── last_reminded_at, reminder_count — Reminder state (cleared on any write)
└── created_at, updated_at
```

The write paths of `create`/`update` publish `task_created`/`task_updated` (Redis events channel, visible from `GLOBAL_ROLES`) — the task board relies on these two events for SSE-driven invalidation refetch, plus a 30s fallback polling.

## Relationship with Other Subsystems

- [[ava_builtins/plugins/ava_fleet/neighbors/neighbors.ava.okf.md|Neighbors]] — notifies old and new owners via `send_message` on owner change
- [[ava_builtins/plugins/ava_fleet/notify.ava.okf.md|Notify]] — can notify user when a task completes
- [[ava_builtins/plugins/ava_fleet/ava_fleet.ava.okf.md|ava_fleet Overview]] — parent fleet plugin index
