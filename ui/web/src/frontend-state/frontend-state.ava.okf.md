---
type: doc
title: Frontend State Management
description: TanStack Query for server state, two Zustand stores for volatile UI/SSE, and localStorage for 8 per-device values; selected conversation state.
tags:
- frontend
---

# Frontend State Management

One writer per cache/flag: TanStack Query owns server data and persisted preferences; Zustand and localStorage retain only the volatile/device state listed below.

## Three Mechanisms Division of Responsibilities

Termination toasts follow the response: `enqueued` means requested, not exited,
even for force. Authoritative roster reads update lifecycle rows; a response-carried `open_tasks`
hint raises the open-tasks notice (display-only, no state write).

| Mechanism | Responsibility | File |
|---|---|---|
| TanStack Query | All **server data** (agent list, status, timeline snapshots, token, agent pages, inspect) **+ persistent UI preferences** (`display.*`/`behavior.*` in `user_settings`, via `useUserSettings`/`useDebouncedSetting`); SSE merges into cache, no polling | `lib/use-*.ts` |
| Zustand `store.ts` | **Volatile UI state** (activeId, composer focus token, mobile drawer, toast, open-tasks notice, search) + cluster coordination (`reconnectNonce`/`clusterStranded`); **not persisted** (`persist` middleware removed) | `lib/store.ts` |
| Zustand `timeline-store.ts` | **SSE-driven timeline state** (items/turnActive/streamingCode/streamingIds/hasMoreOlder); split from `store.ts` so high-frequency code_delta/chat_delta folding only notifies timeline subscribers, not sidebar/spawn/banner | `lib/timeline-store.ts` |
| localStorage | 8 **per-device values** (not synced): active agent (`ava.active.agent_id`), Fleet mobile tab (`ava.fleet.mobileTab`), and library-managed splits (`ava.fleet.split`, `ava.fleet.queue-split`, `ava.memory.graph.split`, `ava.home.columns.desktop`, `ava.home.columns.mobile`, `ava.home.inspector.desktop`) | `use-agents.ts`, `fleet-view.tsx`/`inbox-queue/`, `memory/graph/page.tsx`, `home-layout.tsx` |

`display.*`/`behavior.*` covers: Thinking/Code/Output expand defaults, inspector toggles, sidebar collapse/view mode/sort/stats/show terminated, fleet queue collapse + left panel tab, task graph mode + done/canceled filters, force params (graph + task graph), shell terminal theme, spawn model/preset/reasoning_effort, notification and confirmation toggles, UI language (`display.language`, i18n locale via `i18n/language-provider.tsx`; framework copy only, data plane never translated — `decisions/2026-08-05-frontend-i18n-next-intl.md`) — defaults in `lib/types.ts:USER_SETTING_DEFAULTS`. `content-toggle-store.ts` stays a thin `useUserSettings` wrapper. `inspector-panel-store.ts` is breakpoint-aware (task #793): on desktop (≥ lg) the inspector is a side panel, so `display.inspector_open` stays a DB-backed workspace preference (default closed); on mobile (< lg) it is a full-screen overlay that hides the timeline, so its open state is **per-session volatile state** (`mobileInspectorOpen` in `store.ts`, default closed) and mobile toggles never write the shared setting — opening/closing the overlay on a phone must not yank the desktop panel. `lib/settings-migration.ts` (`<SettingsMigration/>`, once after auth) moves leftover localStorage keys into the DB one by one then deletes them (failure retains the key for retry); the 8 per-device keys are excluded; the old zustand-persist blob (`ava-spawn-prefs`) follows a separate blob-to-field path.

Server data is not mirrored into Zustand — the sidebar reads `useAgents → useQuery`.

The agent tree owns one coherent live snapshot plus minimal ancestor links.
History is a separate paginated/searchable read with only its current page
retained. Selection uses an ID detail read when the agent is outside the live
roster; a terminated or bookmarked agent does not require fetching history.
Lifecycle SSE frames are ID hints, never unversioned full snapshots to replay
onto a newer database result. No terminated-agent cache accumulates events.

The global fold owns read-model repair. Hints coalesce under a fixed deadline;
reads never cancel an already-running repair, and hints received during that
read require a trailing repair. Every stream reconnect requests reconciliation,
including another disconnect inside a previous repair window. Settled query
keys release their scheduling state. The selected conversation trio (timeline /
token-usage / pending) reconciles under the same rules through one composed
read per re-attach (`agent-reconcile.ts`): it joins reads in flight, trails a
second gap during a read, and abandons queued and in-flight work when selection
or visibility is lost.

## Zustand `store.ts` (Pure UI + Cluster Coordination)

- **UI state**: `activeId`, composer focus token, mobile drawer, mobile inspector overlay (`mobileInspectorOpen`), toast, the terminate open-tasks notice (`openTasksNotice`), search. Spawn selections (`behavior.spawn_*`) and sidebar view mode/sort/stats (`display.sidebar_*`, hooks in `lib/sidebar.ts`) are **not here** — DB settings via `useUserSettings`.
- **Cluster coordination**: `reconnectNonce` (sole SSE-reconnect lever) and `clusterStranded` (drives `AppConnectionBanner`). Maintenance ownership is never mirrored into Zustand: Gate's persisted snapshot is the fact, while SSE/poll only trigger a latched Gate reload.

## Selected Timeline State

The selected conversation store and its abortable reads have their own node:
[[ui/web/src/frontend-state/timeline-cache.ava.okf.md|Selected Timeline State]].

## Sticky Bottom Controller (`lib/sticky.ts`)

`createStickyController` is the **sole owner** of the sticky flag — replacing 6 paths that each wrote `stickyRef`. Two core ideas: when pinning, move the baseline together (`notifyPinnedToBottom` makes the pin's own echo read as zero movement, eliminating direction heuristics/grace timers); `lastBottomScrollHeight` witnesses "content growing taller under my feet". At-bottom zone width splits into two thresholds by pointer type (touch/mouse). Callers only issue `requestStick()`/`handleScroll()`/`handleLayoutChange()`, never modify the flag. `scrollToBottomRequest` is the sole forced-scroll signal, bumped on agent switch (`switchThread`) + on send.
