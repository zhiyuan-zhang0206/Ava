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
- **InspectorPanel** — current state, window statistics, and plugin widgets have independent open-only queries and loading/error/retry states. Only the selected agent is queried; rows never prefetch on hover or selection. Queries consume abort signals, discard inactive cache entries, and refetch on selection/manual refresh/60s intervals. Notice events refresh current state, task events refresh widgets; reconnect repairs those current domains immediately while statistics reconcile on their next interval. Compact invalidation remains commit-safe. Each payload must match the selected agent/window. Matching data survives a refresh failure with an amber marker. Closing aborts HTTP reads; gateway shared historical work remains bounded by admission/deadlines rather than by one cancelled waiter. `["agent-pages"]` remains SSE-driven. The composer's context-usage button opens **ContextBreakdown** (`context-breakdown.tsx`, #650)—an anchored panel expanding upward in-place (not a modal), lazily querying `GET /api/agents/{id}/context-breakdown`, color-coded by category (system_prompt/compact_summary/cluster_memory/agent_memory/context_note/user_input/agent_messages/automation/reasoning/output/tool_call/tool_response; legend rows sorted by context share descending, output labeled "Text output", context_note+automation merged into one "System notes" row).
- **HeaderBar** / **ContentToggle** / **ConnectionNotice** / **PendingStrip** — current-agent title plus the Alerts badge and Inspector toggle in the top bar, a single Details tri-state selector in the composer — All / Last / None (`components/content-toggle.tsx`, DB-backed `display.*`, synced across devices), and the inline SSE connection notice. There is no React updating component/state: update hints reload through the always-up Gate, which solely renders maintenance. **CopyButton** (`copy-button.tsx` + `lib/clipboard.ts`)—corner copy for code blocks / command output; `execCommand` fallback when Clipboard API fails.
- **SpawnButton** — cross-machine placement picker: reads `/api/status`, lists only online, non-paused, role=agent-runner machines; 0 spawnable → disabled, 1 → direct send, ≥2 → popover. Spawn model/preset/reasoning_effort selections are DB-backed `behavior.spawn_*` settings. The effort select renders only for a resolved model whose `/api/models` entry publishes an `effort_levels` ladder (no ladder, or no catalog entry yet → no control). Every catalog model also publishes a concrete `reasoning_effort_default` (the registry's per-model tuning value; validated non-empty for spawnable models) — the select shows only concrete ladder values with that default pre-selected, no synthetic "Effort: default" option (task #568); a spawn carries the shown level. The legacy `""` option survives only for a model without a concrete default (none today): it sends no `reasoning_effort`, leaving the provider's own default in force. The stored effort is re-derived against the resolved model's ladder each render, so a level the current model does not offer is never sent. A preset whose config carries `llm_model` / `reasoning_effort` overrides the pickers when selected (task #568); an explicit later pick still wins per-key on the backend merge.

`PageDock` removed—open pages now carried by InspectorPanel's `useAgentPages`.

## Fleet View

The full-screen supervision surface (`components/fleet/`, `app/fleet/page.tsx`) — relationship graph, task graph, task board, unified Inbox queue, shared force controls — has its own node: [[ui/web/src/frontend-components/fleet-view.ava.okf.md|Fleet View]].

## Settings / Auth

- Control page routing see [[ui/web/src/frontend-data-flow/frontend-data-flow.ava.okf.md|Data Flow]] hooks (`useUserSettings`, etc.); `/control/display` goes through the server-side `user_settings` table; the Insights Metrics section is retired (2026-08-04) — `/insights/metrics` now redirects to the Grafana dashboard link.
- **AuthGuard** (`components/auth/`) wraps all pages, showing a login page when unauthenticated; `auth-context` shared via React Context.
