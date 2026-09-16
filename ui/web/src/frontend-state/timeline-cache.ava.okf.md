---
type: doc
title: Per-Thread Timeline Cache
description: The `timeline-store.ts` store — parked-thread buckets (R1/R2/R3), `switchThread` as the sole mover, its memory bounds, and the fetch-on-enter reconcile.
tags:
- frontend
---

# Per-Thread Timeline Cache

`/api/system/all?agents=<active>,<parked…>` carries the active agent's events plus system-level `agent_id=0` signals. Active thread state lives in top-level fields; each **parked** inactive thread goes into `threads: Map<agentId, ThreadTimelineState>`.

- `switchThread` is the **sole mover**: one `set()` parks the outgoing thread, restores the incoming one, flips `activeThreadId`, bumps scroll signals — SSE gate and items never desync.
- In-flight events for a just-parked thread fold into its bucket (R3); parked threads stay in the stream selection (task #1959: parked compacts must still reach the store) — switch-back restores parked state (R2), then fetch-on-enter reconciles the gap.
- **Compact replace edge** (task #3698; user ruling 2026-09-17): a wholesale replace on the ACTIVE thread (reset-window snapshot or SSE-gap heal) bumps `compactReplaceSeq` and records `compactReplaceAgent`; the parked-thread swap does not bump (its replacement renders only after a later switch-back, where fetch-on-enter reconciles). `useCompactHistoryRetention` consumes the edge and re-attaches the newest `display.compact_history_sessions` (default 1; 0 = legacy clear) previous compact segments through the same scroll-up path, so a compact appends the new summary below the retained session instead of clearing the view.
- Memory bound: the `system_prompt` item (item 0.0, ~128KB) is dropped from parked buckets (park + snapshot fold) — it is re-sent in every `timeline_snapshot` and was the largest retained object in the page heap (~40MB of copies with the fleet active); `switchThread` restores the full item from the React Query snapshot on switch-back, and the active thread keeps its own copy for the expandable card.
- Load priority: parked bucket > React Query snapshot (hot restore, no flash) > cold (empty until fetch lands).
- Aw-Snap memory bound: there is NO fleet-wide timeline prefetch — it retained one full timeline per fleet agent (the ~128KB `system_prompt` plus history) in the React Query cache for gcTime=30min, the dominant renderer-heap source (~445 agents × 2-3 prompt copies ≈ 88MB). The `["timeline", agentId]` query fires only for agents actually opened, so live prompt copies ≈ visited + active threads, never fleet size. Snapshots carry no system-prompt special-casing: incremental snapshots never include 0.0 (message 0 is below the publish cursor); full-window snapshots (spawn / compact / claim fallback) include it when the tail window holds it. The merge keeps one copy per thread (id-replace); parked buckets keep theirs under the LRU cap (MAX_PARKED_THREADS=32).
- LRU cap `MAX_PARKED_THREADS = 32` (`timeline-store.ts:206`); token fields stay out of buckets (cached under `["token-usage", agentId]`). `token-usage` carries per-model soft/hard compact thresholds (`context-meter.tsx` gauge ticks); the composer button opens `context-breakdown.tsx` — anchored in-place panel (not a modal), lazy-loading `["context-breakdown", agentId]` on open (`GET /api/agents/{id}/context-breakdown`).
- **fetch-on-enter**: switching back to a cached thread triggers a background reconcile refetch even with a live parked bucket — buckets are not freshness guarantees (events missed during disconnection silently expire them); `mergeSnapshotWithStreaming` returns the same reference when unchanged, so the refetch costs zero renders. **stale-while-error**: during reconcile in-flight/failed, loaded content stays shown (same as `useTasks`/`TaskGraph`: on poll failure retain last data, `StaleBadge` marks "stale"; failure shown only on cold start with no data).
- `foldEvent` pure-folds one thread's events; `processSseEvent` folds high-frequency SSE in a single `set()` (code_delta one chunk per event), avoiding cascading re-renders.

## Relationship to Other Nodes

- [[ui/web/src/frontend-state/frontend-state.ava.okf.md|Frontend State Management]] — the three-mechanism split this store belongs to; the sticky bottom controller and the volatile store slots live there.
