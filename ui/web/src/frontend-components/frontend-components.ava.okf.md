---
type: doc
title: Frontend Components
description: Component catalog — conversation view (sidebar/composer/inspector), settings & auth, plus pointers to the Fleet View and Timeline View nodes.
tags:
- frontend
---

# Frontend Components

## Conversation View (`app/page.tsx`)

Three layers: `HomePage` (read-only toast) → `HomeShell` (`useAgents`, activeId, lifecycle actions) → `HomeContent` (in `AgentEventStreamProvider`, with `useTimeline`/`useTokenUsage`/`usePendingMessages`). `HomeLayout` mounts after breakpoint settlement, adds draggable Agent Tree/Inspector splits with isolated desktop/compact autosave keys, and keeps phone overlays.

- **AgentSidebar** — agent list, spawn tree / flat dual view (DB-backed user setting); search + terminated toggle + status-dot quick toggle + sorting merged into one toolbar; collapsed is a blank strip with only an expand button. Default silent (RCS): list stably sorted by agent ID descending (status/activity changes never re-sort); status dot, activity line, awaiting-reply badge are opt-in (`display.show_agent_status` / `display.show_activity_line` / `notification.awaiting_reply`, default off). SSE real-time status.
- **TimelineView** (`components/timeline/`) — the conversation timeline renderer; item kinds, dividers, cross-compact paging and deep collapse have their own node: [[ui/web/src/frontend-components/timeline-view.ava.okf.md|Timeline View]].
- **Composer** — message input (file upload, content blocks, multimodal image_url); `disabled|idle|busy` states; busy reveals Stop (inserts a persistent cancel inbound). One logical submit owns one client message id across timeout/retry and tab persistence. A bounded but still unresolved delivery becomes an explicit read-only "Delivery unconfirmed" state with separate **Retry same message** and **Send another anyway** decisions; storage failures degrade to an in-memory identity instead of blocking the send/spinner cleanup.
- **InspectorPanel** — current state, window statistics, and plugin widgets have independent open-only queries and loading/error/retry states. Only the selected agent is queried; rows never prefetch on hover or selection. Queries consume abort signals, retain each agent's snapshot for the 30-minute switch window (`lib/switch-budget.ts`), and refetch on selection/manual refresh/60s intervals; a back-switch inside the window renders the retained snapshot immediately (no skeleton) and revalidates only the two live reads (the widget set is selection-invariant). Notice events refresh current state, task events refresh widgets; reconnect repairs those current domains immediately while statistics reconcile on their next interval. Compact invalidation remains commit-safe. Each payload must match the selected agent/window. Matching data survives a refresh failure with an amber marker. Closing aborts HTTP reads; gateway shared historical work remains bounded by admission/deadlines rather than by one cancelled waiter. `["agent-pages"]` remains SSE-driven. The composer's context-usage button opens **ContextBreakdown** (`context-breakdown.tsx`, #650)—an anchored panel expanding upward in-place (not a modal), lazily querying `GET /api/agents/{id}/context-breakdown`, color-coded by category (system_prompt/compact_summary/cluster_memory/agent_memory/context_note/user_input/agent_messages/automation/reasoning/output/tool_call/tool_response; legend rows sorted by context share descending, output labeled "Text output", context_note+automation merged into one "System notes" row). Since task #4023 (P4-3) the same body also renders as a standalone card (**ContextBreakdownCard**) below the run page's chart and window metrics — same query and rendering, thresholds read from the response's mirrored fields (`max_input_tokens`/`soft_compact_tokens`/`hard_compact_tokens`; the composer's live values stay with the collapsed `ContextMeter`).
- **HeaderBar** / **ContentToggle** / **ConnectionNotice** / **PendingStrip** — current-agent title plus the Alerts badge and Inspector toggle in the top bar, a single Details tri-state selector in the composer — All / Last / None (`components/content-toggle.tsx`, DB-backed `display.*`, synced across devices), and the inline SSE connection notice. There is no React updating component/state: update hints reload through the always-up Gate, which solely renders maintenance. **CopyButton** (`copy-button.tsx` + `lib/clipboard.ts`)—corner copy for code blocks / command output; `execCommand` fallback when Clipboard API fails.
- **SpawnButton** — cross-machine placement and model/preset/effort selection; creation availability and selected-agent reason: [[ui/web/src/frontend-components/creation-availability.ava.okf.md|Creation Availability]].

`PageDock` removed—open pages now carried by InspectorPanel's `useAgentPages`.

## Fleet View

The full-screen supervision surface (`components/fleet/`, `app/fleet/page.tsx`) — relationship graph, task graph, task board, unified Inbox queue, shared force controls — has its own node: [[ui/web/src/frontend-components/fleet-view.ava.okf.md|Fleet View]].

## Settings / Auth

- Control page routing see [[ui/web/src/frontend-data-flow/frontend-data-flow.ava.okf.md|Data Flow]] hooks (`useUserSettings`, etc.); `/control/display` goes through the server-side `user_settings` table; the Insights Metrics section is retired (2026-08-04) — `/insights/metrics` now redirects to the Grafana dashboard link.
- **AuthGuard** (`components/auth/`) wraps all pages, showing a login page when unauthenticated; `auth-context` shared via React Context.

The single-agent run page keeps session selection and tracing warnings above a viewport-height timeline; custom dates live in a closed disclosure. Route navigation and pending timeline reads display matching skeletons. Inline raw summaries on run and compare charts start with a capped three-line preview and an accessible expand/collapse control.

## Run timeline reader

The single-run page's `RunTimelineWorkspace` keeps a 440px reader beside its
independently scrolling main column at viewport widths >=1280px, filling the
space below the page header. `RunTimelineChart` owns selection and portals one
existing turn/layer/message panel into that reader; closing returns to the
localized hint. New selections reset reader scroll; full-text expansion survives
placement changes. Below 1280px details remain below the chart. Hover previews,
focus actions, chain chips and on-demand budgeted message queries stay with the
chart. Compare lanes retain their existing detail layout and the shared
`RawSummaryBand` three-line (48px text) collapsed cap.

The route skeleton shares `RunTimelineControls` with the loaded page. The range
label reserves space before data arrives; session notices have a fixed,
scrollable viewport (62px mobile, 42px from 640px), so warning arrival does not
move the chart. Long or simultaneous notices remain reachable by scrolling.
