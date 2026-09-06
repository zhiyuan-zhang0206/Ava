---
type: doc
title: Fleet View
description: The full-screen supervision surface (`components/fleet/`, `app/fleet/page.tsx`)—relationship graph, task graph, task board, the unified Inbox notice queue, and the force-parameter panel the two graphs share.
tags:
- frontend
- fleet
---

# Fleet View

## What it is

Full-screen supervision plane: judge 10-20 agents without opening any conversation. Read-only except InboxQueue's reply / Send / Dismiss (require_response) and Mark read (FYI) — the resurrect button was removed 2026-07-31. Desktop ≥lg left-right draggable split, mobile full-screen tabs.

## Sub-components

- **GraphView** (`graph-view.tsx`) — force-directed relationship graph: spawn/fork/resurrect lineage + attenuated message traffic. Nodes use the same three public lifecycle statuses as the sidebar; independent `liveness_state=offline` overrides the lifecycle color with muted gray, so an internal starting/restarting transition can never masquerade as a healthy idling node.
- **TaskGraph** (`task-graph.tsx`) — task tree (child tasks only, Done/Canceled visibility toggle, count consistent with board). Left pane: Graph/Task tabs (persisted).
- **InboxQueue** (`inbox-queue/`) — right pane single unified stream: all open notices (require_response decisions + FYI) sorted by priority→blocking→created_at, grouped via `groupByTaskSubtree` using the seven-day metadata-only task summary window; items differ only on the action side (shared `OpenNoticeDetail` branches on `require_response`: reply box+Send+Dismiss / Mark read); resolved history behind a collapsed disclosure; counts rolled up from agent snapshots (`notices_awaiting_response` + `unread_notice_count`). A conversation-to-Fleet link carries `agent_id`; after the first notice load, the queue scrolls to and briefly highlights that agent's first displayed open notice without changing display order, or stays at the default position when none exists. Collapsing the panel is the DB-backed `display.fleet_queue_collapsed` of `fleet-view.tsx`.
- **TaskKanban** (`task-kanban.tsx`) — task board (owner column shows agent label, stale-while-error retains last data).
- **ForceControls** (`force-controls.tsx`) — shared force-directed slider panel for Graph View / Task Graph; `useForceParams` DB-backed (`useDebouncedSetting` throttles writes). **Independent tuning per canvas** (user ruling 2026-08-10 #1127): the Agent Graph reads `display.graph_force_params`, the Task Graph `display.task_force_params.v2` (2026-08-05 had both share one key — the .v2 key was already wired in settings-migration but never populated; the 8/10 ruling splits them). The agent graph's edge group also carries the edge-weight thickness switch (`display.graph_edge_weight`, default on; off renders uniform thin edges — attraction stays weight-aware). Edge styling is a single gray for every kind (lineage + message), distinguished by opacity/width only.

## Relationship to Other Nodes

- [[ui/web/src/frontend-components/frontend-components.ava.okf.md|Frontend Components]] — the catalog this surface was split out of; the conversation view and settings/auth components live there.
- [[ui/web/src/frontend-state.ava.okf.md|State Management]] — where the `display.*` settings above are persisted, and which selections stay per-device instead.
