---
type: doc
title: Cluster View
description: The multi-agent page - cluster curves over one agent lane per spawn-tree member on a shared zoomable axis, message connectors between lanes, and a lane that opens into the agent's run timeline.
tags:
- frontend
---

# Cluster View

`/insights/cluster?root=&from=&to=` (`components/cluster-view/`, entry link in the Insights page) shows a root agent and everything it spawned or forked over a window. The selection lives in the URL (`cluster-selection.ts`); the window is the extent, and one viewport moves inside it with the run timeline's gestures and math (`timeline-model.ts`: `zoomViewport`, `panViewport`, `RunTimelineAxis`), so wheel / pinch zooms at the cursor, drag or horizontal scroll pans.

**Rows, top to bottom.** Four curves (`cluster-curves.tsx`: cost per minute stacked by agent in lane order, active agents, messages per minute, queue time p50 over p95), the shared axis, then a lane per agent (`cluster-lanes.tsx`). A lane has the understanding nodes of one level above a bar of LLM activity, with lifecycle markers across it. Lanes follow the spawn lineage; the triangle folds a lane's subtree. Metric definitions are in [[services/derived/insights/cluster/docs/cluster.ava.okf.md|cluster view reads]].

**Connectors.** `cluster-edges.tsx` draws each message from the sender's lane at its send time to the receiver's lane at its read time (`claimed_at`); an unclaimed one is a dashed vertical at its send time. Ends under a folded lane are drawn on the folded ancestor's lane, and a message inside a folded subtree is dropped (`drawnEdges`). Nothing says which message the receiver handled or what woke it. Opening a lane dims the connectors that do not touch it.

**Reads follow the view.** The page holds three on-demand reads (curves, lanes, messages) for a loaded window a little wider than the viewport; when the view leaves it or zooms in far (`nextFetchWindow`), the reads move after the view rests for 250 ms, so the bucket width and the lanes' detail match the screen. Data sits at its own times, so what is loaded stays right while the next read is in flight. The level selector re-reads the lanes at a chosen level (auto by default).

**Open lane.** Clicking an agent number opens its run timeline in place (`cluster-lane-expansion.tsx`): `RunTimelineRows` and the node / unit details, reused unchanged, on the cluster view's axis (the frame is pulled out by its own padding so tracks line up). That is the only checkpoint-backed read, for one agent at a time.
