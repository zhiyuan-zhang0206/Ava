---
type: doc
title: Frontend UI
description: Ava's web interface — Next.js 16 + React 19 + Tailwind 4 + shadcn/ui. Browser directly connects to Gateway, with SSE push and bounded snapshot polling. Stack + pages/routes + Provider composition.
tags:
- frontend
---

# Frontend UI

Ava's web user interface — Next.js 16 (App Router) + React 19 + Tailwind CSS 4 + shadcn/ui (Radix). Runs on port 3000, browser **directly connects** to Gateway API (`<host>:8000`, `credentials: include` with session cookie), does not go through Next.js rewrites proxy (Turbopack dev proxy buffers SSE). Stack policy: `conventions/frontend-stack.md` (shadcn/Radix/Tailwind mainstream only).

## Tech stack

| Layer | Selection |
|---|---|
| Framework | Next.js 16 (App Router) |
| UI | React 19 |
| Styling | Tailwind CSS 4 |
| Component library | shadcn/ui (Radix primitives) |
| Font | Inter for UI chrome + Geist Mono for technical content |
| Theme | next-themes (class strategy, follow system) |
| Server data | TanStack React Query |
| Real-time comms | EventSource (SSE) |

## Pages / Routes

```
/                             Chat view — sidebar + timeline + composer (homepage)
/fleet                        Fleet supervision view — relationship graph + Task graph + unified Inbox queue (InboxQueue, replaces old Decisions/Reviews dual queues)
/login                        Login page
/memory/graph                 Memory knowledge graph visualization
/shell/[agentId]/[sessionId]  Terminal session (session backend proxy)
/control                      Control vertical long page — write/admin surface (renamed from /settings; observation surface split to /insights)
                              First-level section anchors: guide/config/presets/display/plugins/mcp/skills/schedules/okf-graph (9th section, added 2026-07-24: button opens /api/okf/graph — D3 force-directed .ava.okf.md doc graph, rebuilt from the current doc tree per request)
                              Top header: chat link + title; left two-level anchor nav (ControlNav) + right single scroll container (replaces TabBar)
                              Structure + data queries all ready on first screen (not expanded on scroll); IntersectionObserver only pauses polling after leaving viewport (enabled:visible,_visibility.tsx, saves connection budget)
                              Anchor jump instant (scrollIntoView behavior:auto)
                              Old /control#status, #metrics (and #metrics-* sub-anchors) deep-link → router.replace to /insights corresponding anchor
                              Section components are also bare routes: control/{guide,config,display,presets,schedules,inventory,skills}/page.tsx
                              inventory/page.tsx exports PluginsInventory/McpInventory (shared /api/inventory query) as Plugins+MCP sections
/insights                     Observation surface (read) — Status + Ops + Alerts sections, same shell as Control (sharing ControlNav + single scroll container INSIGHTS_SCROLL_ID)
                              Continuous polling usage (Status 15s; update-check only on entry/manual re-check); cluster Restart/Update buttons are right in Status header, "observe health → act in place"
                              Ops links to Grafana; Alerts is live via SSE. Retired Metrics bookmarks transition through insights/metrics/page.tsx to Ops when Grafana is reachable.
                              Section components are also bare routes: insights/{status,ops}/page.tsx; insights/alerts/page.tsx redirects to the Alerts anchor
/insights/run/[agentId]       Run-level tracing view — a direct, shareable linear turn track for one agent, with prioritized event connectors and click-through turn details (time, usage, cost, model, execs, anomalies). It reads the gateway's bounded run session, offers explicit start/end and zoom windows, requests one-hour buckets up front for explicit windows of at least six hours, and switches after a turn response above 400 rows. Insights and the active inspector link here.
/control#skills               Installed skills read-only table: name/layer origin (core=repo|plugin|machine=user|untracked)/enabled/local drift (GET /api/skills, single-machine gateway local read)
/settings, /settings/*        Redirect to /control, /control/* (next.config.ts redirects, preserves old bookmarks/links)
```

## Provider composition (`app/layout.tsx` → `components/providers.tsx`)

`<html lang="zh-CN">` → `Providers` (QueryClientProvider → AuthProvider → EventStreamProvider → [fold owner + `SettingsMigration`] → ThemeProvider → `AppConnectionBanner` + `ToastHost`) → `AuthGuard` → page.

- **QueryClient**: `staleTime` default 5min, `gcTime` 30min, `refetchOnWindowFocus` off (SSE-driven queries set `staleTime: Infinity`; bounded snapshots opt into polling explicitly). Sidebar stats consumers share one QueryClient-level 30s poll coordinator, so responsive/header/footer observers cannot mint independent intervals. A 401 is never retried and globally invalidates the session so `AuthGuard` redirects to `/login` and unmounts polling observers (Task #1326).
- **EventStreamProvider** sits above the route tree: global `/api/system` broadcast persists across page navigation (one persistent EventSource).
- **Fold owner** (`useFoldOwner`, inside `EventStreamProvider`): the SOLE root writer — one subscriber folds every global-broadcast event into the query caches (`["agents", "live"]` + `["agents", "terminated"]` — both scopes always seeded, / `["notices"]` / `["agent-pages"]` / `["tasks"]` / `["fleet-graph"]` families, debounced per family) and runs the central reconnect reconcile. Hooks only read their keys now.
- **ToastHost**: root-level renderer for the store's toast slot, so error toasts reach the user on every route (not just Home).
- **SettingsMigration** (#657): empty-rendering component that runs once after auth, migrates legacy localStorage preferences into DB `user_settings` then deletes the keys.
- **AppConnectionBanner** (#648): root-mounted; drives cluster health polling (`useClusterHealth`), mirrors SSE status into store (`ConnectionNotice`), stranded-cluster recovery banner (the only root banner, requires operator action); self-gated by `useAuth().status`. All Providers self-gate on auth state—no outer auth guard layer.

## Core principles

- **SSE first**: agents/timeline/token/pages are folded into React Query with no polling. Snapshot-only surfaces poll only while visible/enabled and share a page-level cadence where they have multiple consumers.
- **No optimistic write**: state changes await SSE event confirmation; lifecycle actions only flip `isPending`.
- **Single writer**: each cache/flag has exactly one writer.

## Three deeper topics

- [[frontend-state.ava.okf.md|State management]] — three mechanisms division of labor + per-thread timeline cache + sticky controller
- [[ui/web/src/frontend-data-flow/frontend-data-flow.ava.okf.md|Data flow]] — dual SSE Provider architecture + hook directory
- [[frontend-components.ava.okf.md|Components]] — chat view / Fleet view / settings page components
- [[frontend-typography.ava.okf.md|Typography]] — family boundary, named size scale, and audited deviations

## Entry points

- `ui/web/src/app/layout.tsx` — root layout
- `ui/web/src/app/page.tsx` — homepage (HomePage → HomeShell → HomeContent)
- `ui/web/src/components/providers.tsx` — Provider composition + QueryClient
- `ui/web/src/lib/store.ts` — Zustand store (pure UI + cluster coordination, not persisted)
- `ui/web/src/lib/timeline-store.ts` — Zustand store (SSE-driven timeline state, independent from `store.ts`)
- `ui/web/src/lib/useEventStream.tsx` — SSE Provider
