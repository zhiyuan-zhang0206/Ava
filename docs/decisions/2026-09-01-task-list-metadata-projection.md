---
title: Task list metadata projection
---

# Task list metadata projection

The list summary omits all task text, including truncated previews. The Inbox
Queue uses this metadata-only projection with its seven-day grouping window,
because it needs ownership and tree structure but never renders task text.

The Task Graph and Kanban board request the full projection explicitly. They
render task descriptions and results, so their user-selectable graph window
(default 24 hours) remains the bounded full-text path.
