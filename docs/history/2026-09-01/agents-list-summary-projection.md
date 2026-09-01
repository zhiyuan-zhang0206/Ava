---
title: Agent list summary projection
---

# Agent list summary projection

`GET /api/agents` gained an explicit `fields=summary` projection for roster
consumers. It uses a separate SQL statement, so full-only columns do not cross
the database or gateway boundary before being discarded.

The summary keeps every value used by the web roster, SDK, CLI, and MCP list
tools, including fork lineage, liveness, response-required notices, unread
notice counts, and the computed vision capability. It excludes the fork
checkpoint identifier, last probe timestamp, and raw configuration overlay.
The query selects only the overlay's model scalar to preserve the existing
vision-capability calculation.

`fields=full` remains the compatibility default and single-agent detail and
SSE snapshots remain full. This isolates the smaller projection to high-cardinality
list reads without changing diagnostic or event contracts.
