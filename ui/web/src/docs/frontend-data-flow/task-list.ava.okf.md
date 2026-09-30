---
type: doc
title: Task list data flow
description: useTasks selects full task rows for the graph and Kanban board, and metadata summaries for the Inbox Queue.
tags:
- frontend
- tasks
---

# Task list data flow

`useTasks` chooses the task projection at each caller. The Task Graph and
Kanban board fetch `GET /api/tasks?fields=full`, retaining description and
results for their rendered text. The Inbox Queue fetches
`GET /api/tasks?window=7d&fields=summary`, carrying only task metadata for
task-subtree grouping.

The Task Graph persists a graph-view window (default 24h); the Inbox Queue's
seven-day window keeps active tasks and their ghost ancestors for grouping.

`task_created` and `task_updated` SSE events invalidate the task cache after a
two-second debounce. A constant 30-second reconciliation poll continues after
errors; loaded task data remains visible while the hook separately reports the
stale state.
