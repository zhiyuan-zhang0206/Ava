---
type: doc
title: Frontend Data Flow (SSE + hooks)
description: Two SSE Providers — global /api/system broadcast + active-agent /api/system/all stream with hidden-tab polling; #648 connection resilience; React Query hook directory.
tags:
- frontend
- sse
---

# Frontend Data Flow (SSE + hooks)

Server data enters UI via React Query cache, kept live by SSE while visible; hidden tabs close the connection and invalidate the active agent's REST snapshots every 7s. SSE connects directly to FastAPI (not through Next rewrites — Turbopack dev proxy buffers SSE).

## Two SSE Providers (`lib/useEventStream.tsx`)

| Provider / hook | Endpoint | Content | Subscribers |
|---|---|---|---|
| `EventStreamProvider` / `useEventStream` | `/api/system` | **Global broadcast**: cross-agent low-frequency lifecycle (spawn/update/label, page open/close, notice/task changes, `cluster_update_started`) | **Fold owner** reconciles `["agents","live"]` + opt-in `["agents","terminated"]`, notices, pages, tasks, and fleet graph; reconnect repair is scoped and throttled |
| `AgentEventStreamProvider` / `useAgentEventStream` | `/api/system/all?agents=<activeId>` | **Active-agent throttled stream**: selected agent plus `agent_id=0` system events, batched (`data: [{...}]`), throttled ≤10 push/s | `useTimeline`, `useTokenUsage`, `usePendingMessages` |

The agent stream is connected while authenticated and visible; `activeId` re-keys its URL (null → unfiltered endpoint). Hidden tabs: the provider passes `null` to `useSseConnection` (closes EventSource) and emits `ConnectionEvent {type: "poll"}` every 7s; the three subscribers invalidate `timeline`/`token-usage`/`pending` for the active agent. Visible again: interval cleared, SSE reopens, the `open` event reconciles REST state. `isEventForThread` remains a defensive gate. Multiple hooks share one EventSource; `withCredentials` carries the session cookie through gateway auth.

## Connection resilience (#648)

- **Half-dead watchdog**: 45s without any frame (even heartbeats) = socket stuck in OPEN (graceful restart / proxy hop) → `bumpReconnect()` forces a clean reopen. Server sends a heartbeat frame roughly every 15s as liveness.
- **CLOSED auto-reconnect**: unauthenticated streams never open; a CLOSED stream probes `/api/auth/check` — invalid session flips the auth context (AuthGuard → /login) and stays closed until login; valid session or failed probe → capped backoff reopen (`retryTimer` single-flight, `retryNonce` effect lever, 1s doubled to 30s, reset on open; separate from the global `reconnectNonce`). `onerror` dispatches CLOSED/CONNECTING/OPEN (unknown → throw); `ConnectionEvent` = open/poll/reconnecting/closed/parse-failed. AlertsProvider mirrors gate/probe/backoff with independent state.
- **Cluster update Gate reload + reconnect**: global `cluster_update_started` is a hint emitted only after the persistent UI generation exists; `AppConnectionBanner` asks the current URL to reload through Gate. The root-mounted, auth-independent `GateMaintenanceProvider` polls Gate's same-origin `GET /__ava/deploy-state` as the missed-SSE fallback. Both share a module-level latch, so their race navigates once. Neither renders or times maintenance. The authenticated `/api/cluster/status` poll in `useClusterHealth` still distinguishes stranded pause and reconnects SSE/refetches agents on the real paused true→false gateway-bounce edge.
- For the two system streams, watchdog and cluster update use the global store `reconnectNonce`; each CLOSED retry uses its own local `retryTimer` + `retryNonce` and does not re-key the other Provider. AlertsProvider has its own reconnect/watchdog and retry state.

## Hook directory (`lib/use-*.ts`)

| hook | data |
|---|---|
| `useAgents` | SQL-bounded live roster + opt-in terminated history; fold/SSE keeps both caches live |
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
| `GateMaintenanceProvider` | auth-independent Gate snapshot reload hint |
| `useClusterHealth` | cluster paused polling + SSE reconnect coordination |
| `useNotices` | the unified Inbox feed — one request carries the open queue (FYI + awaiting) and a keyset page of resolved history (R4 layer 2 single contract); notice_* events invalidate-refetch |
| ~~`usePrefetchTimelines`~~ | removed (Aw-Snap fix) — the fleet-wide full-timeline prefetch retained one ~128KB system prompt + history per agent for gcTime=30min, the dominant renderer-heap source; timelines now fetch on demand when an agent is opened |
| `useThrottledStreaming` | streaming increment throttled batching |
| `useUserSettings` | server-side user preferences (`user_settings` table) |
| `useMediaQuery` / `useIsLarge` | responsive breakpoints |


## Frontend telemetry (user modeling, #1092)

Tracked interactions flow **one-way** to the gateway — never into React Query / SSE:

`lib/telemetry.ts` `track(element, {page, key, value, dedupe})` → in-memory buffer (dedupe 2s per page/element/key; Web Vitals and API timing opt out; under the 100/min per-tab and 200 pending caps) → batched `POST /api/frontend-telemetry` (sendBeacon on hide/pagehide, fetch+keepalive otherwise) → gateway validates (shape 422 / 64KB 413 / per-session 120/min backstop) → one `frontend_interaction` event per interaction (category=telemetry, source=user) → `events` table → Grafana core panels.

Instrumented points: page views plus native FCP/final LCP/CLS/INP (`lib/telemetry-page-view.tsx` + `lib/web-vitals.ts`, mounted in AuthGuard's authenticated branch), API requests slower than 800ms (`lib/api.ts`, normalized numeric path segments), composer send-to-first-turn-start latency (`lib/interaction-timing.ts`), agent lifecycle actions (`lib/use-agent-actions.ts` onSuccess), message send/stop (`components/composer.tsx`), and every user_settings change (`lib/use-user-settings.ts` setSetting). No sensitive content: `element` is a closed union, keys are bounded normalized identifiers, and `value` is a ≤64-char scalar.
