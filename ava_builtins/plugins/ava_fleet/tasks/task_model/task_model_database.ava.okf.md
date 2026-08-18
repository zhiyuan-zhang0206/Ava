---
type: doc
title: Task Model — Database Mapping & Task Tree
description: "agent_tasks table mapping, _COLS constant, _row_to_task, and the parent-child task tree structure with recursive queries"
tags:
- fleet
- tasks
- data-model
- database
---

# Database Mapping & Task Tree

## Database Mapping

Table name `agent_tasks`, column order corresponds one-to-one with `Task` fields, kept in sync via the `_COLS` constant:

```python
_COLS = "id, parent_id, title, description, results, status, owner, created_by, created_at, updated_at, remind_interval_seconds, last_reminded_at, reminder_count, priority"
```

`_row_to_task(row)` unpacks in this order into `Task(*row)`.

## Parent-Child Task Tree

```
#1 Deploy new version (root)
├── #5 Write release script → owner=#238
├── #6 Update documentation   → owner=#405
│   └── #9 Translate to Chinese → owner=#405 (open)
└── #7 Run integration tests  → owner=#312 (done)
```

- `list(parent=1, recursive=True)` returns #5, #6, #7, #9 (the entire subtree, excluding #1 itself).
- `list(parent=1)` returns only direct children #5, #6, #7.
