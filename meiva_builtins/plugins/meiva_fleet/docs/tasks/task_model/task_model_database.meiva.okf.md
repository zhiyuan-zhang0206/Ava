---
type: doc
title: Task Model — Database Mapping & Task Tree
description: "agent_tasks table mapping, TASK_COLUMNS, task_from_row, and the parent-child task tree structure with recursive queries"
tags:
- fleet
- tasks
- data-model
- database
---

# Database Mapping & Task Tree

## Database Mapping

Table name `agent_tasks`; `base.agents.tasks.model.Task` owns the field order.
`TASK_COLUMNS` is derived from `dataclasses.fields(Task)`. The Fleet SDK imports
that constant and `task_from_row` directly from `base.agents.tasks.model`.
`task_from_row(row)` renders the established cluster-zone timestamps and unpacks
the fields in that order into `Task(*row)`.

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
