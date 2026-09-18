---
type: doc
title: Hook directory
description: Per-hook data catalog — what each `use-*` hook reads, folds, and refetches, and how it behaves on reconnect / hidden tabs.
tags:
- frontend
- sse
---

# Hook directory (`lib/use-*.ts`)

| hook | data |
|---|---|
| `useAgents` | SQL-bounded live roster + always-fetched terminated history (merged roster = raw lineage for the spawn tree; show-terminated is render-only); fold/SSE keeps both caches live |
| `useFleetAgents` | `/fleet` read-only agents (pure-read shares `AGENTS_QUERY_KEY` cache) |
| `useFleetGraph` | Fleet relationship graph (GraphView data source); SSE invalidation + 30s reconciliation poll, served from the backend's 60s whole-response cache |
| `useTasks` | [[ui/web/src/frontend-data-flow/task-list.ava.okf.md|Task list data flow]] |
| `useTimeline` | selected timeline tail + live SSE fold, with abortable older-history paging; the retained window (30min) seeds a switch back and the shared composed reconcile refreshes it; see [[ui/web/src/frontend-state/frontend-state.ava.okf.md|State management]] |
| `useTokenUsage` | selected context occupancy (abortable cold read + SSE token_usage; a retained snapshot seeds a switch back and the shared composed reconcile refreshes it) |
| `useCompactHistoryRetention` | consumes the store's compact-replace edge (`compactReplaceSeq`): re-attaches the newest `display.compact_history_sessions` previous segments above the new compact summary through the scroll-up fetch path (task #3698) |
| `useAgentPages` | single agent opened pages (InspectorPanel, SSE folds page_opened/closed into cache, replaces deleted PageDock/use-fleet-pages) |
| `useAllPages` (#655) | fleet-wide opened pages fetched once + SSE incremental fold (Inbox attaches associated page links to notices, avoids N+1 per-agent requests) |
| `usePendingMessages` | selected pending inbound queue with abortable reads and bounded queue-event hint repair (the open gap belongs to the shared composed reconcile); the page hides items already visible in the timeline (takeover capture, #3683) |
| `useAgentReconcile` (`lib/agent-reconcile.ts`) | the conversation trio's one composed re-attach read (`GET /api/agents/{id}/conversation-snapshot`), shared by `useTimeline`/`useTokenUsage`/`usePendingMessages`: joins reads in flight (write last), trails a hint that arrives during a read, drops a snapshot superseded by a newer write, aborts with the last reader, and falls back to three per-domain invalidations on failure (task #3900) |
| Run timeline page | one on-demand React Query read of `GET /api/agents/{id}/run-timeline` per agent/window/session/level; it does not subscribe or poll, requests turns first for a server-selected session, requests one-hour buckets up front for an explicit window of at least six hours, and falls back to buckets before rendering a turn response above 400 rows. The compare view's per-lane reads pass a reduced strip cap (`messages_max` from `display.run_timeline_compare_messages_max`, default 200; the server clamps to its ceiling) and hold each lane's previous response as the placeholder across window changes — `useQueries` replaces observers by query hash, so `keepPreviousData` alone would blank a lane mid-refetch (task #4023 P4-4). The Context breakdown card adds one **agent-scoped** read of `GET /api/agents/{id}/context-breakdown`, decoupled from the window/session keys — a timeline refetch never refetches it (task #4023 P4-3) |

Message POSTs are bounded across both headers and body consumption. A timeout,
transport loss, 429, or 5xx is an ambiguous outcome: the client looks up the
same `Idempotency-Key` through `/messages/reconcile`, may resubmit the original
body once under that same key, and never silently generates a replacement key.
The returned `inbound_id` is the durable receipt; exhaustion leaves the draft in
an explicit unconfirmed state for same-message retry or deliberate abandonment.
| `GateMaintenanceProvider` | auth-independent Gate snapshot reload hint |
| `useClusterHealth` | cluster paused polling + SSE reconnect coordination |
| `useNotices` | the unified Inbox feed — one request carries the open queue (FYI + awaiting) and a keyset page of resolved history (R4 layer 2 single contract); notice_* events invalidate-refetch |
| ~~`usePrefetchTimelines`~~ | removed (Aw-Snap fix) — the fleet-wide full-timeline prefetch retained one ~128KB system prompt + history per agent for gcTime=30min, the dominant renderer-heap source; timelines now fetch on demand when an agent is opened |
| `useThrottledStreaming` | streaming increment throttled batching |
| `useUserSettings` | server-side user preferences (`user_settings` table) |
| `useMediaQuery` / `useIsLarge` | responsive breakpoints |
