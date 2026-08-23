---
type: doc
title: Frontend Data Flow (SSE + hooks)
description: Two SSE Providers — global /api/system broadcast + full /api/system/all throttled broadcast; #648 connection resilience (half-dead watchdog + CLOSED exponential backoff reconnect + cluster update reconnect); React Query hook directory.
tags:
- frontend
- sse
---

# Frontend Data Flow (SSE + hooks)

Server data enters UI via React Query cache, kept live by SSE **folded into cache** (no polling). SSE connects directly to FastAPI (not through Next rewrites — Turbopack dev proxy buffers SSE).

## Two SSE Providers (`lib/useEventStream.tsx`)

| Provider / hook | Endpoint | Content | Subscribers |
|---|---|---|---|
| `EventStreamProvider` / `useEventStream` | `/api/system` | **Global broadcast**: cross-agent low-frequency lifecycle (spawn/update/label, page open/close, notice_posted/notice_resolved, **#663** task_created/task_updated; `GLOBAL_ROLES` total **9** roles, `shared/live_events.py`) | **Fold owner** (`useFoldOwner` — sole root writer; folds into `["agents"]`/`["notices"]`/`["agent-pages"]`/`["tasks"]`/`["fleet-graph"]` families, debounced 2s per family; central reconnect reconcile throttled to 1/30s), readers: `useAgents`, `useAgentPages`, `useAllPages`, `useNotices`, `useTasks`, `useFleetGraph` |
| `AgentEventStreamProvider` / `useAgentEventStream` | `/api/system/all` | **Full throttled broadcast**: every event of every agent, batched (`data: [{...}]`), throttled ≤25 push/s, no server-side filtering | `useTimeline`, `useTokenUsage`, `usePendingMessages` |

The full stream `/api/system/all` is **always connected** (not bound to activeId), frontend uses `isEventForThread` to internally filter by thread. Shared mechanism `useSseConnection` is identical for both, only the URL differs — multiple hooks share the same EventSource (old design had one connection per hook, resulting in N concurrent streams + weird middle banner). EventSource uses `withCredentials` to carry session cookie through gateway auth.

## Connection resilience (#648)

- **Half-dead watchdog**: 45s without any frame (even heartbeats) = socket stuck in OPEN (graceful restart / proxy hop) → `bumpReconnect()` forces a clean reopen. Server sends a heartbeat frame roughly every 15s as liveness.
- **CLOSED auto-reconnect**: `EventSource` on non-2xx responses (cluster-paused 503, 500, 401) enters CLOSED and the browser **never** auto-retries — relying solely on native behavior would cause a permanent stall. `onerror` explicitly dispatches on three `readyState` states (CLOSED/CONNECTING/OPEN, no catch-all default, unknown value directly throws); on CLOSED uses a local `retryNonce` for single-flight, exponential backoff reopen (`RECONNECT_BASE_MS=1s` doubled capped at `RECONNECT_MAX_MS=30s`, successful open resets count), separate from the global `reconnectNonce`, not interfering with the other Provider's connection. `ConnectionEvent` four states: `open`/`reconnecting`/`closed`/`parse-failed`.
- **Cluster update reconnect** (`use-cluster-health`): polls `paused` flag (5s interval, query key `["status"]` — same key de-duplicates with other polls like SpawnButton/agent-row), after rollout finishes (true→false) immediately reconnects + refetches, without waiting for the 45s watchdog; also feeds `clusterUpdating` to `AuthGuard` (driving full-screen `UpdatingPage` during unauthenticated rollout, rather than redirecting to `/login`), `clusterStranded` to `AppConnectionBanner` (mounted at `Providers` root to protect all pages, not just homepage, renders stranded-recovery banner). Another poll path `/api/cluster/status` (bypassing the paused 503 middleware) distinguishes normal rollout from stranded pause.
- Three triggers (watchdog / CLOSED retry / cluster update) all converge to the store's `reconnectNonce` (the latter two share it, CLOSED retry additionally has an independent `retryNonce` affecting only its own connection); `useSseConnection` lists them in effect deps, a bump tears down old EventSource and opens new.

## Hook directory (`lib/use-*.ts`)

| hook | data |
|---|---|
| `useAgents` | agent list + lifecycle actions — pure reader of the `["agents"]` cache; the fold owner writes it (no poll, no optimistic) |
| `useFleetAgents` | `/fleet` read-only agents (pure-read shares `AGENTS_QUERY_KEY` cache) |
| `useFleetGraph` | Fleet relationship graph (GraphView data source); SSE invalidation + 30s reconciliation poll, served from the backend's 60s whole-response cache |
| `useTasks` | Task Graph/Kanban data source; SSE task_created/task_updated invalidate (2s debounce) + 30s fallback poll (constant interval, does not stop on failure); **stale-while-error** — poll failure continues to serve last data, `error` separately exposed for `StaleBadge` hint |
| `useTimeline` | timeline items (merged three sources: React Query snapshot + SSE fold + reload merge; switching back to a cached thread triggers fetch-on-enter background reconcile, see [[frontend-state.ava.okf.md|State management]]) |
| `useTokenUsage` | context window occupancy (React Query historical value + SSE token_usage) |
| `useAgentPages` | single agent opened pages (InspectorPanel, SSE folds page_opened/closed into cache, replaces deleted PageDock/use-fleet-pages) |
| `useAllPages` (#655) | fleet-wide opened pages fetched once + SSE incremental fold (Inbox attaches associated page links to notices, avoids N+1 per-agent requests) |
| `usePendingMessages` | pending inbound messages count |

Message POSTs are bounded across both headers and body consumption. A timeout,
transport loss, 429, or 5xx is an ambiguous outcome: the client looks up the
same `Idempotency-Key` through `/messages/reconcile`, may resubmit the original
body once under that same key, and never silently generates a replacement key.
The returned `inbound_id` is the durable receipt; exhaustion leaves the draft in
an explicit unconfirmed state for same-message retry or deliberate abandonment.
| `useClusterHealth` | cluster paused polling + SSE reconnect coordination |
| `useNotices` | the unified Inbox feed — one request carries the open queue (FYI + awaiting) and a keyset page of resolved history (R4 layer 2 single contract); notice_* events invalidate-refetch |
| ~~`usePrefetchTimelines`~~ | removed (Aw-Snap fix) — the fleet-wide full-timeline prefetch retained one ~128KB system prompt + history per agent for gcTime=30min, the dominant renderer-heap source; timelines now fetch on demand when an agent is opened |
| `useThrottledStreaming` | streaming increment throttled batching |
| `useUserSettings` | server-side user preferences (`user_settings` table) |
| `useMediaQuery` / `useIsLarge` | responsive breakpoints |


## Frontend telemetry (user modeling, #1092)

Tracked interactions flow **one-way** to the gateway — never into React Query / SSE:

`lib/telemetry.ts` `track(element, {page, key, value})` → in-memory buffer (dedupe 2s per page/element/key, 100 events/min per tab, 200 cap) → batched `POST /api/frontend-telemetry` (sendBeacon on hide/pagehide, fetch+keepalive otherwise) → gateway validates (shape 422 / 64KB 413 / per-session 120/min backstop) → one `frontend_interaction` event per interaction (category=telemetry, source=user) → `events` table → Grafana core panels (interactions / top elements / page views / settings changes).

Instrumented points: page views (`lib/telemetry-page-view.tsx`, mounted in AuthGuard's authenticated branch), agent lifecycle actions (`lib/use-agent-actions.ts` onSuccess), message send/stop (`components/composer.tsx`), and every user_settings change (`lib/use-user-settings.ts` setSetting — covers all settings/layout/preference edits in one hook). No sensitive content: `element` is a closed union, `value` a ≤64-char scalar.
