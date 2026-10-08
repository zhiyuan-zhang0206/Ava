---
type: doc
title: Frontend Components
description: Component catalog — conversation view (sidebar/composer/inspector), settings & auth, plus pointers to the Fleet View, Timeline View and Run Timeline nodes.
tags:
- frontend
---

# Frontend Components

## Conversation View (`app/page.tsx`)

Three layers: `HomePage` (read-only toast) → `HomeShell` (`useAgents`, activeId, lifecycle actions) → `HomeContent` (in `AgentEventStreamProvider`, with `useTimeline`/`useTokenUsage`/`usePendingMessages`). `HomeLayout` mounts after breakpoint settlement, adds draggable Agent Tree/Inspector splits with isolated desktop/compact autosave keys, and keeps phone overlays.

- **AgentSidebar** — agent list, spawn tree / flat dual view (DB-backed user setting); search + terminated toggle + status-dot quick toggle + sorting merged into one toolbar; collapsed is a blank strip with only an expand button. Default silent (RCS): list stably sorted by agent ID descending (status/activity changes never re-sort); status dot, activity line, awaiting-reply badge are opt-in (`display.show_agent_status` / `display.show_activity_line` / `notification.awaiting_reply`, default off). SSE real-time status.
- **TimelineView** (`components/timeline/`) — the conversation timeline renderer; item kinds, dividers, cross-compact paging and deep collapse have their own node: [[ui/web/src/docs/frontend-components/timeline-view.ava.okf.md|Timeline View]].
- **Composer** — message input (file upload, content blocks, multimodal image_url); `disabled|idle|busy` states; busy reveals Stop (inserts a persistent cancel inbound). One logical submit owns one client message id across timeout/retry and tab persistence. An unresolved delivery preserves the editable draft and reports an unconfirmed receipt through the existing error toast. Normal Send reuses the client message id for an unchanged draft; editing the content or attachments starts a new logical submission; storage failures degrade to an in-memory identity instead of blocking the send/spinner cleanup.
- **InspectorPanel** — current state, window statistics, and plugin widgets have independent open-only queries and loading/error/retry states. Only the selected agent is queried; rows never prefetch on hover or selection. Queries consume abort signals, retain each agent's snapshot for the 30-minute switch window (`lib/switch-budget.ts`), and refetch on selection/manual refresh/60s intervals; a back-switch inside the window renders the retained snapshot immediately (no skeleton) and revalidates only the two live reads (the widget set is selection-invariant). Notice events refresh current state, task events refresh widgets; reconnect repairs those current domains immediately while statistics reconcile on their next interval. Compact invalidation remains commit-safe. Each payload must match the selected agent/window. Matching data survives a refresh failure with an amber marker. Closing aborts HTTP reads; gateway shared historical work remains bounded by admission/deadlines rather than by one cancelled waiter. `["agent-pages"]` refetches on page events. The composer's context-usage button opens **ContextBreakdown** (`context-breakdown.tsx`, #650)—an anchored panel expanding upward in-place (not a modal), lazily querying `GET /api/agents/{id}/context-breakdown`, color-coded by category (system_prompt/compact_summary/cluster_memory/agent_memory/context_note/user_input/agent_messages/automation/reasoning/output/tool_call/tool_response; legend rows sorted by context share descending, output labeled "Text output", context_note+automation merged into one "System notes" row). Since task #4023 (P4-3) the same body also renders as a standalone card (**ContextBreakdownCard**) below the run page's chart and window metrics — same query and rendering, thresholds read from the response's mirrored fields (`max_input_tokens`/`soft_compact_tokens`/`hard_compact_tokens`; the composer's live values stay with the collapsed `ContextMeter`).
- **HeaderBar** / **ContentToggle** / **ConnectionNotice** / **PendingStrip** — current-agent title plus the Alerts badge and Inspector toggle in the top bar, a single Details tri-state selector in the composer — All / Last / None (`components/content-toggle.tsx`, DB-backed `display.*`, synced across devices), and the inline SSE connection notice. There is no React updating component/state: update hints reload through the always-up Gate, which solely renders maintenance. **CopyButton** (`copy-button.tsx` + `lib/clipboard.ts`)—corner copy for code blocks / command output; `execCommand` fallback when Clipboard API fails.
- **SpawnButton** — cross-machine placement and model/preset/effort selection; creation availability and selected-agent reason: [[ui/web/src/docs/frontend-components/creation-availability.ava.okf.md|Creation Availability]].

Warnings / Errors stats card: [[ui/web/src/docs/frontend-components/alert-classes.ava.okf.md|Alert Classes]].

Plugin statistics use full-width rows: primary `value`, secondary `detail`,
both wrap and preserve line breaks. No provider parsing. The sidebar popover
fits the viewport and scrolls within available height; empty/error/stale states
and update-age tooltips remain.

## Code blocks

`PythonCode` keeps its highlighter (`prism-react-renderer`, ~85KB) out of
the `/` route's initial bundle via a plain dynamic `import()`, but warms
that chunk proactively: `CodeHighlighterPreloader` mounts on every page
with code blocks (home timeline, `/control/schedules`) and fetches it once
idle; a code block's own collapsed toggle (`CardHeader`, the schedules row
expand button) also fetches it on pointer-enter/focus, ahead of the click
that expands it. Not yet resolved → plain unhighlighted text (copy control
+ streaming cursor still present, no blank body); already prefetched → the
first render is already highlighted, no flash. Tokenization still only
runs when a code block renders or its source changes.

## Fleet View

The full-screen supervision surface (`components/fleet/`, `app/fleet/page.tsx`) — relationship graph, task graph, task board, unified Inbox queue, shared force controls — has its own node: [[ui/web/src/docs/frontend-components/fleet-view.ava.okf.md|Fleet View]].

## Settings / Auth

- Control page routing see [[ui/web/src/docs/frontend-data-flow/frontend-data-flow.ava.okf.md|Data Flow]] hooks (`useUserSettings`, etc.); `/control/display` goes through the server-side `user_settings` table; the Insights Metrics section is retired (2026-08-04) — `/insights/metrics` now redirects to the Grafana dashboard link.
- **AuthGuard** (`components/auth/`) wraps all pages, showing a login page when unauthenticated; `auth-context` shared via React Context.

The run timeline page (`/insights/run/{id}`) is its own node: [[ui/web/src/docs/frontend-components/run-timeline/run-timeline.ava.okf.md|Run Timeline]]. The multi-agent page (`/insights/cluster`) is another: [[ui/web/src/docs/frontend-components/cluster-view/cluster-view.ava.okf.md|Cluster View]].
