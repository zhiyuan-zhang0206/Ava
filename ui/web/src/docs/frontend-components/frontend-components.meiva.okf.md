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

`HomeContent` keeps a failed timeline read visible with the original error and
manual Retry after automatic retries finish. A cold failure stays an error;
a failed refresh keeps loaded messages visible. Retry is disabled during the
read. `useTimeline` exposes the query error and refetch action without replacing
the cache or changing global retries. Separately retained checkpoint history
does not enter the live conversation through this surface.

- **AgentSidebar** — agent list, spawn tree / flat dual view (DB-backed user setting); search + terminated toggle + status-dot quick toggle + sorting merged into one toolbar; collapsed is a blank strip with only an expand button. Default silent (RCS): list stably sorted by agent ID descending (status/activity changes never re-sort); status dot, activity line, awaiting-reply badge are opt-in (`display.show_agent_status` / `display.show_activity_line` / `notification.awaiting_reply`, default off). SSE real-time status. Every home entry expands once: the one-time reset lives in `HomeShell` (which survives layout-frame switches while the sidebar remounts) and waits for the persisted settings, so a cold load with `collapsed=true` still expands.
- **TimelineView** (`components/timeline/`) — the conversation timeline renderer; item kinds, dividers, cross-compact paging and deep collapse have their own node: [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]].
- **Composer** — message input (file upload, content blocks, multimodal image_url); `disabled|idle|busy` states; busy reveals Stop (observes the agent's native work, then cancels it). One logical submit owns one client message id across timeout/retry and tab persistence. An unresolved delivery preserves the editable draft and reports an unconfirmed receipt through the existing error toast. Normal Send reuses the client message id for an unchanged draft; editing the content or attachments starts a new logical submission; storage failures degrade to an in-memory identity instead of blocking the send/spinner cleanup. `busy` unions `status='running'` with the SSE turn-in-progress signal, which alone flips at every mid-turn `llm_done`. A message invoking several commands (`/recap … /compact`) is sent whole — the server expands them inside that one inbound.
- **InspectorPanel** — the selected agent's current state, window statistics and plugin widgets read independently, only while open, and never on hover or selection. Each section loads, fails and retries on its own, renders nothing when empty, and its payload must match the selected agent/window before it shows; matching data survives a refresh failure with an amber marker. Sections (run link → Cost + Activity → the runtime sections), the responsive close surfaces, and the per-agent retention and refresh ownership are in [[ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md|Inspector Panel]] and [[ui/web/src/docs/frontend-components/inspector/queries-and-refresh.meiva.okf.md|reads and refresh]]. The composer's context-usage button opens **ContextBreakdown** (`context-breakdown.tsx`, #650) — code-split until expansion while the live meter stays in the initial bundle — and the same body renders as a card on the run page: [[ui/web/src/docs/frontend-components/context-breakdown.meiva.okf.md|Context breakdown presentation]].
- **HeaderBar** / **ContentToggle** / **ConnectionNotice** / **PendingStrip** — current-agent title plus the Alerts badge and Inspector toggle in the top bar, a single Details tri-state selector in the composer — All / Last / None (`components/content-toggle.tsx`, DB-backed `display.*`, synced across devices), and the inline SSE connection notice; the strip lists only user-originated queued messages — an agent message, a watcher / shell / schedule wake or a system note never appears (the gateway stamps each row's `sender`, a closed category of its `source` tag, so the frontend selects without parsing tags) — and hides messages the timeline already shows (a takeover-captured row stays pending until the executor's ACK). There is no React updating component/state: update hints reload through the always-up Gate, which solely renders maintenance. **CopyButton** (`copy-button.tsx` + `lib/clipboard.ts`)—corner copy for code blocks / command output; `execCommand` fallback when Clipboard API fails.
- **SpawnButton** — cross-machine placement and model/effort selection; creation availability and selected-agent reason: [[ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md|Creation Availability]].

Warnings / Errors stats card: [[ui/web/src/docs/frontend-components/alert-classes.meiva.okf.md|Warning and Error Counts]].

Plugin statistics use full-width rows: primary `value`, secondary `detail`,
both wrap and preserve line breaks. No provider parsing. The sidebar popover
fits the viewport and scrolls within available height; empty/error/stale states
and update-age tooltips remain.

## Code blocks

`PythonCode`'s highlighter loading — the lazy import, its proactive warm-up
and prefetch triggers, and when tokenization runs — has its own node:
[[ui/web/src/docs/frontend-components/code-blocks.meiva.okf.md|Code blocks]].

## Fleet View

The full-screen supervision surface (`components/fleet/`, `app/fleet/page.tsx`) — relationship graph, task graph, task board, unified Inbox queue, shared force controls — has its own node: [[ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md|Fleet View]].

## Settings / Auth

- Control page routing see [[ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md|Data Flow]] hooks (`useUserSettings`, etc.); `/control/display` goes through the server-side `user_settings` table.
- **AuthGuard** (`components/auth/`) wraps all pages, showing a login page when unauthenticated; `auth-context` shared via React Context; on mount, only when the URL fragment carries a one-time `#ticket=` login link, it lazily loads `lib/auth/login-ticket.ts` (kept out of the first-load bundle), which clears the fragment before posting the ticket; `/api/auth/check` then decides, and a refused link leaves the login form (the cluster password still works).

The run timeline page (`/insights/run/{id}`) is its own node: [[ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md|Run Timeline]].
