---
title: Task list summary projection
---

# Task list summary projection

The web task list now requests a server-side summary projection: each task
carries 300-character description and results previews, but the query never
selects either full text column. The full row remains the default for existing
clients and for the PATCH response, so this list optimization does not change
the established detail contract.

The summary shares the existing window and ghost-ancestor filtering after row
selection. The Inbox Queue uses the seven-day window; its active tasks and their
ancestor chains remain present for subtree grouping.

Update: the preview-bearing summary was superseded by the metadata-only
projection documented in [[docs/history/2026-09-01/task-list-metadata-projection.md|Task list metadata projection]].
