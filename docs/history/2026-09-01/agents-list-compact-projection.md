---
title: Agent list compact projection
---

# Agent list compact projection

The summary roster projection deliberately retains enough state for the SDK,
MCP, and frontend. That remains too broad for `ava agents ls`, which renders
only an agent id, lifecycle status, and label across all historical rows.

`GET /api/agents?fields=compact` therefore uses a separate three-column SQL
statement over `agents_meta` and `agents`. It has no LATERAL inbound lookup,
notice subquery, or configuration extraction. The CLI alone requests it;
`fields=full` remains the compatibility default and `fields=summary` remains
the roster projection for its existing consumers.

On the 2026-09-01 read-only production sample, 5,218 all-scope rows serialized
to 381,791 compact JSON bytes, compared with 2,802,663 full bytes and
2,460,642 summary bytes.

Superseded by [Agent list machine visibility](../2026-09-04/agents-list-machine-visibility.md).
