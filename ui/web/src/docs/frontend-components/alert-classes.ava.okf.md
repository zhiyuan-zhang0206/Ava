---
type: doc
title: Warning and Error Counts
description: The compact, windowed Warning / Error event totals in the sidebar statistics panel, separate from formal Grafana alerts.
tags:
- frontend
---

# Warning and Error Counts

The Warnings / Errors card in the stats popover (`agent-sidebar/footer.tsx`) displays
`warnings / errors` from `GET /api/stats/dashboard` over the selected statistics
window. These are event totals; `errors` includes critical events. The card is a
compact cell like the other statistics, with no nested class/sample list.
Changing the window uses the same loading and mismatch placeholders as its peers.

The event-class diagnostics and dismissal APIs remain available to operators;
they neither replace these totals nor become formal alert data.
The backend contract: [[gateway/cluster/docs/ops-surfaces.ava.okf.md|Ops Surfaces]].

Formal Grafana alerts have their own header badge and Insights history. Their
SSE stream invalidates TanStack Query snapshots, so the server owns ordering,
filters and uncapped counts. All unresolved instances remain visible regardless
of age; the history window selects recently resolved episodes by recovery time.
Grouping and recovery belong to Grafana / Alertmanager, not frontend time buckets.
