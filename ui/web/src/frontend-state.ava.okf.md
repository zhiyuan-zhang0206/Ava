---
type: doc
title: Frontend State Management
description: TanStack Query for server state, two Zustand stores for volatile UI/SSE, and localStorage for 8 per-device values; per-thread timeline caching.
tags:
- frontend
---

# Frontend State Management

One writer per cache/flag: TanStack Query owns server data and persisted preferences; Zustand and localStorage retain only the volatile/device state listed below.

## Three Mechanisms Division of Responsibilities

Termination toasts follow the response: `enqueued` means requested, not exited,
even for force. Only SSE updates lifecycle rows.

| Mechanism | Responsibility | File |
|---|---|---|
| TanStack Query | All **server data** (agent list, status, timeline snapshots, token, agent pages, inspect) **+ persistent UI preferences** (`display.*`/`behavior.*` in `user_settings`, via `useUserSettings`/`useDebouncedSetting`); SSE merges into cache, no polling | `lib/use-*.ts` |
| Zustand `store.ts` | **Volatile UI state** (activeId, composer focus token, mobile drawer, toast, search) + cluster coordination (`reconnectNonce`/`clusterStranded`); **not persisted** (`persist` middleware removed) | `lib/store.ts` |
| Zustand `timeline-store.ts` | **SSE-driven timeline state** (items/turnActive/streamingCode/streamingIds/hasMoreOlder/parked threads); split from `store.ts` so high-frequency code_delta/chat_delta folding only notifies timeline subscribers, not sidebar/spawn/banner | `lib/timeline-store.ts` |
| localStorage | 8 **per-device values** (not synced): active agent (`ava.active.agent_id`), Fleet mobile tab (`ava.fleet.mobileTab`), and library-managed splits (`ava.fleet.split`, `ava.fleet.queue-split`, `ava.memory.graph.split`, `ava.home.columns.desktop`, `ava.home.columns.mobile`, `ava.home.inspector.desktop`) | `use-agents.ts`, `fleet-view.tsx`/`inbox-queue/`, `memory/graph/page.tsx`, `home-layout.tsx` |

`display.*`/`behavior.*` covers: Thinking/Code/Output expand defaults, inspector toggles, sidebar collapse/view mode/sort/stats/show terminated, fleet queue collapse + left panel tab, task graph mode + done/canceled filters, force params (graph + task graph), shell terminal theme, spawn model/preset/reasoning_effort, notification and confirmation toggles, UI language (`display.language`, i18n locale via `i18n/language-provider.tsx`; framework copy only, data plane never translated — `decisions/2026-08-05-frontend-i18n-next-intl.md`) — defaults in `lib/types.ts:USER_SETTING_DEFAULTS`. `content-toggle-store.ts` stays a thin `useUserSettings` wrapper. `inspector-panel-store.ts` is breakpoint-aware (task #793): on desktop (≥ lg) the inspector is a side panel, so `display.inspector_open` stays a DB-backed workspace preference (default closed); on mobile (< lg) it is a full-screen overlay that hides the timeline, so its open state is **per-session volatile state** (`mobileInspectorOpen` in `store.ts`, default closed) and mobile toggles never write the shared setting — opening/closing the overlay on a phone must not yank the desktop panel. `lib/settings-migration.ts` (`<SettingsMigration/>`, once after auth) moves leftover localStorage keys into the DB one by one then deletes them (failure retains the key for retry); the 8 per-device keys are excluded; the old zustand-persist blob (`ava-spawn-prefs`) follows a separate blob-to-field path.

Server data is not mirrored into Zustand — the sidebar reads `useAgents → useQuery`.

## Zustand `store.ts` (Pure UI + Cluster Coordination)

- **UI state**: `activeId`, composer focus token, mobile drawer, mobile inspector overlay (`mobileInspectorOpen`), toast, search. Spawn selections (`behavior.spawn_*`) and sidebar view mode/sort/stats (`display.sidebar_*`, hooks in `lib/sidebar.ts`) are **not here** — DB settings via `useUserSettings`.
- **Cluster coordination**: `reconnectNonce` (sole SSE-reconnect lever) and `clusterStranded` (drives `AppConnectionBanner`). Maintenance ownership is never mirrored into Zustand: Gate's persisted snapshot is the fact, while SSE/poll only trigger a latched Gate reload.

## `timeline-store.ts` + Per-Thread Timeline Cache (R1/R2/R3)

`/api/system/all?agents=<active>,<parked…>` carries the active agent's events plus system-level `agent_id=0` signals. Active thread state lives in top-level fields; each **parked** inactive thread goes into `threads: Map<agentId, ThreadTimelineState>`.

- `switchThread` is the **sole mover**: one `set()` parks the outgoing thread, restores the incoming one, flips `activeThreadId`, bumps scroll signals — SSE gate and items never desync.
- In-flight events for a just-parked thread fold into its bucket (R3); parked threads stay in the stream selection (task #1959: parked compacts must still reach the store) — switch-back restores parked state (R2), then fetch-on-enter reconciles the gap.
- Memory bound: the `system_prompt` item (item 0.0, ~128KB) is dropped from parked buckets (park + snapshot fold) — it is re-sent in every `timeline_snapshot` and was the largest retained object in the page heap (~40MB of copies with the fleet active); `switchThread` restores the full item from the React Query snapshot on switch-back, and the active thread keeps its own copy for the expandable card.
- Load priority: parked bucket > React Query snapshot (hot restore, no flash) > cold (empty until fetch lands).
- Aw-Snap memory bound: there is NO fleet-wide timeline prefetch — it retained one full timeline per fleet agent (the ~128KB `system_prompt` plus history) in the React Query cache for gcTime=30min, the dominant renderer-heap source (~445 agents × 2-3 prompt copies ≈ 88MB). The `["timeline", agentId]` query fires only for agents actually opened, so live prompt copies ≈ visited + active threads, never fleet size. Snapshots carry no system-prompt special-casing: incremental snapshots never include 0.0 (message 0 is below the publish cursor); full-window snapshots (spawn / compact / claim fallback) include it when the tail window holds it. The merge keeps one copy per thread (id-replace); parked buckets keep theirs under the LRU cap (MAX_PARKED_THREADS=32).
- LRU cap `MAX_PARKED_THREADS = 32` (`timeline-store.ts:206`); token fields stay out of buckets (cached under `["token-usage", agentId]`). `token-usage` carries per-model soft/hard compact thresholds (`context-meter.tsx` gauge ticks); the composer button opens `context-breakdown.tsx` — anchored in-place panel (not a modal), lazy-loading `["context-breakdown", agentId]` on open (`GET /api/agents/{id}/context-breakdown`).
- **fetch-on-enter**: switching back to a cached thread triggers a background reconcile refetch even with a live parked bucket — buckets are not freshness guarantees (events missed during disconnection silently expire them); `mergeSnapshotWithStreaming` returns the same reference when unchanged, so the refetch costs zero renders. **stale-while-error**: during reconcile in-flight/failed, loaded content stays shown (same as `useTasks`/`TaskGraph`: on poll failure retain last data, `StaleBadge` marks "stale"; failure shown only on cold start with no data).
- `foldEvent` pure-folds one thread's events; `processSseEvent` folds high-frequency SSE in a single `set()` (code_delta one chunk per event), avoiding cascading re-renders.

## Sticky Bottom Controller (`lib/sticky.ts`)

`createStickyController` is the **sole owner** of the sticky flag — replacing 6 paths that each wrote `stickyRef`. Two core ideas: when pinning, move the baseline together (`notifyPinnedToBottom` makes the pin's own echo read as zero movement, eliminating direction heuristics/grace timers); `lastBottomScrollHeight` witnesses "content growing taller under my feet". At-bottom zone width splits into two thresholds by pointer type (touch/mouse). Callers only issue `requestStick()`/`handleScroll()`/`handleLayoutChange()`, never modify the flag. `scrollToBottomRequest` is the sole forced-scroll signal, bumped on agent switch (`switchThread`) + on send.
