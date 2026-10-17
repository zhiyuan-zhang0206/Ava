---
type: doc
title: Frontend State Management
description: TanStack Query for server state, two Zustand stores for volatile UI/SSE, and localStorage for 7 per-device values; selected conversation state.
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
| Zustand `store.ts` | **Volatile UI state** (activeId, composer focus token, mobile drawer, toast, open-tasks notice, search) + cluster coordination (`reconnectNonce`); **not persisted** (`persist` middleware removed) | `lib/store.ts` |
| Zustand `timeline-store.ts` | **SSE-driven timeline state** (items/turnActive/streamingCode/streamingIds/hasMoreOlder and the compact display buffer); split from `store.ts` so high-frequency code_delta/chat_delta folding only notifies timeline subscribers, not sidebar/spawn/banner | `lib/timeline-store.ts` |
| localStorage | 7 **per-device values** (not synced): active agent (`meiva.active.agent_id`), Fleet mobile tab (`meiva.fleet.mobileTab`), and library-managed splits (`meiva.fleet.split`, `meiva.memory.graph.split`, `meiva.home.columns.context`, `meiva.home.columns.mobile`, `meiva.home.context.desktop`) | `use-agents.ts`, `fleet-view.tsx`, `memory/graph/page.tsx`, `home-layout.tsx` |

`display.*`/`behavior.*` covers: Thinking/Code/Output expand defaults, inspector toggles, sidebar collapse/view mode/sort/stats/show terminated, fleet queue collapse + left panel tab, task graph mode + done/canceled filters, force params (graph + task graph), shell terminal theme, spawn model/reasoning_effort, notification and confirmation toggles, `behavior.replySuggestions`, UI language (`display.language`, i18n locale via `i18n/language-provider.tsx`; framework copy only, data plane never translated — `docs/decisions/frontend/browser/2026-08-05-frontend-i18n-next-intl.md`) — defaults in `lib/types.ts:USER_SETTING_DEFAULTS`. `content-toggle-store.ts` stays a thin `useUserSettings` wrapper.

## Inspector open state

`inspector-panel-store.ts` is breakpoint-aware (task #793): on desktop (≥ lg) the inspector is the right panel's secondary view, so `display.inspector_open` stays a DB-backed workspace preference (default closed) that selects that view; on mobile (< lg) the reading surfaces are full-screen overlays that hide the timeline, so the inspector's open state is **per-session volatile state** (`mobileInspectorOpen` in `store.ts`, default closed) and mobile toggles never write the shared setting — opening or closing on a phone must not yank the desktop view. The statistics window select (`inspectorHours`) is session state and is not persisted.

Server data is not mirrored into Zustand — the sidebar reads `useAgents → useQuery`. The composer's drafted-reply ghost text is hook-local volatile state with one writer ([[gateway/agents/docs/reply-suggestion/reply-suggestion.meiva.okf.md]]).

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
- **Cluster coordination**: `reconnectNonce` is the SSE-reconnect lever. `AppConnectionBanner` reads connection health directly. No maintenance or update state is mirrored into Zustand; a reload while the app is down is answered by Gate's unavailable page.

## Selected Timeline State

The selected conversation store and its abortable reads have their own node:
[[ui/web/src/docs/frontend-state/timeline-cache.meiva.okf.md|Selected Timeline State]].

## Sticky Bottom Controller (`lib/sticky.ts`)

`createStickyController` is the **sole owner** of the sticky flag — replacing 6 paths that each wrote `stickyRef`. Two core ideas: when pinning, move the baseline together (`notifyPinnedToBottom` makes the pin's own echo read as zero movement, eliminating direction heuristics/grace timers); `lastBottomScrollHeight` witnesses "content growing taller under my feet". At-bottom zone width splits into two thresholds by pointer type (touch/mouse). Callers only issue `requestStick()`/`handleScroll()`/`handleLayoutChange()`, never modify the flag. `scrollToBottomRequest` is the sole forced-scroll signal, bumped on agent switch (`switchThread`) + on send — those two force pins also hand the controller a fresh upward run (`notifyPinnedToBottom(view, freshRun)`; a stale run would otherwise release following again on the first post-pin twitch, before the reply could be followed). Automatic (streaming-growth) pins keep the run.

## History-Entry Scroll Memory

Per-container reading positions survive back/forward through the history
entry's `bfcacheId`; the save/restore path has its own node:
[[ui/web/src/docs/frontend-state/scroll-memory.meiva.okf.md|History-Entry Scroll Memory]].

## Shared user context

The document reader, notification area, inspector view and file-change repair are
owned by [[ui/web/src/docs/frontend-state/shared-context.meiva.okf.md|Shared user context]].
