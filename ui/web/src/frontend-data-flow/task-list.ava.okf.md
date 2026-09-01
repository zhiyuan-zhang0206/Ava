---
type: doc
title: Task list data flow
description: useTasks reads compact task summaries for the task graph, Kanban board, and Inbox Queue.
tags:
- frontend
- tasks
---

# Task list data flow

`useTasks` fetches `GET /api/tasks?fields=summary`, so every row carries
server-truncated description and results previews rather than full task text.
The caller controls its activity window: the Task Graph persists a graph-view
window (default 24h), while the Inbox Queue uses `7d` for task-subtree grouping.

`task_created` and `task_updated` SSE events invalidate the task cache after a
two-second debounce. A constant 30-second reconciliation poll continues after
errors; loaded task data remains visible while the hook separately reports the
stale state.
