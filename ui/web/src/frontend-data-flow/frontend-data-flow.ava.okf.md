---
type: doc
title: Frontend Data Flow (SSE + hooks)
description: Profile-shared authenticated SSE transport for system, active-agent, and alerts streams; visible leader ownership, hidden-page release, and connection resilience.
tags:
- frontend
- sse
---

# Frontend Data Flow (SSE + hooks)

Server data enters UI via React Query cache, kept live by SSE while visible; hidden pages leave the transport and reconcile when visible again. SSE connects directly to FastAPI (not through Next rewrites — Turbopack dev proxy buffers SSE).

## Stream ownership (`lib/sse-share.ts`)

**At most one set of SSE connections per browser profile.** Each visible,
authenticated page requests the exclusive `ava-ui-sse` Web Lock. The lock holder
owns the real EventSources for `/api/system`, `/api/alerts/stream`, and the
selected-agent union `/api/system/all`; other pages own no gateway SSE sockets.
The leader relays raw frames (including heartbeats) and open/reconnecting/closed
state over `BroadcastChannel("ava-ui-sse")`. Each page still folds its own cache.
The selected-agent feed is filtered back to that page's active ID before its
subscribers see it. Followers mirror state but leave auth probing, retry, and
EventSource construction to the leader. A new leader's open state triggers each
page's existing reconciliation.

Visible pages announce their selected agent; the leader sorts and unions IDs,
debouncing URL changes by 250ms. A 15s ping asks pages to renew interest; the
leader prunes missing pages after 45-50s (45s TTL, 5s sweep). Hide aborts a
pending lock request or releases a held lock and closes its sources. With no
visible pages, no streams remain. If BroadcastChannel or Web Locks is missing,
the Providers retain their original per-page EventSource path.

## Two SSE Providers (`lib/useEventStream.tsx`)

| Provider / hook | Endpoint | Content | Subscribers |
|---|---|---|---|
| `EventStreamProvider` / `useEventStream` | `/api/system` | **Global broadcast**: cross-agent low-frequency lifecycle (spawn/update/label, page open/close, notice/task changes, `cluster_update_started`) | **Fold owner** reconciles `["agents","live"]` + `["agents","terminated"]` (both always seeded — terminated rows are the spawn-tree lineage joints), notices, pages, tasks, and fleet graph; reconnect repair is scoped, coalesced, and guaranteed after each gap |
| `AgentEventStreamProvider` / `useAgentEventStream` | `/api/system/all?agents=<active>` | **Active-agent throttled stream**: only the selected agent plus `agent_id=0` system events, batched (`data: [{...}]`), throttled ≤10 push/s | `useTimeline`, `useTokenUsage`, `usePendingMessages` |

The detail stream exists only while at least one authenticated, visible page has
a selected agent. Clearing the last selection closes it; union changes replace it.
No inactive or previously visited agent remains subscribed. Hidden pages do not replace SSE
with polling. Reopening refreshes the selected timeline, token usage, and
pending messages with one composed reconcile read (`/conversation-snapshot`,
agent-reconcile.ts) and repairs the global read models. Alerts retain a
separate domain stream.
Multiple hooks share each page's subscription, and disposed connections discard late
callbacks. `withCredentials` carries the session cookie through gateway auth.

## Connection resilience (#648)

`lib/gateway-origin.ts` resolves the same API origin for browser fetch/SSE and
the server-rendered CSP. `AVA_BROWSER_ORIGIN` is an optional exact HTTPS entry,
injected by the canonical frontend build: visits there use same-origin gateway
routes behind an HTTP/2-capable proxy, while existing direct frontend URLs
retain gateway-port routing. The gate login follows the same entry selection.
The proxy sends gateway routes directly to the gateway and frontend/navigation
requests through the gate, preserving its maintenance generation boundary.

- **Half-dead watchdog**: 45s without any frame (even heartbeats) = socket stuck in OPEN (graceful restart / proxy hop) → the leader reopens it; a follower requests repair through the channel. Server sends a heartbeat frame roughly every 15s as liveness.
- **CLOSED auto-reconnect**: unauthenticated streams never open; the leader probes `/api/auth/check` after a CLOSED stream — invalid session flips auth state and stays closed until login; valid session or failed probe → single-flight capped backoff reopen (1s doubled to 30s, reset on open). Followers only mirror connection state. The per-page fallback retains its existing retry timers and `retryNonce` levers. `onerror` dispatches CLOSED/CONNECTING/OPEN (unknown → throw); `ConnectionEvent` = open/reconnecting/closed/parse-failed.
- **Cluster update Gate reload + reconnect**: global `cluster_update_started` is a hint emitted only after the persistent UI generation exists; `AppConnectionBanner` asks the current URL to reload through Gate. The root-mounted, auth-independent `GateMaintenanceProvider` polls Gate's same-origin `GET /__ava/deploy-state` as the missed-SSE fallback. Both share a module-level latch, so their race navigates once. Neither renders or times maintenance. The authenticated `/api/cluster/status` poll in `useClusterHealth` still distinguishes stranded pause and reconnects SSE/refetches agents on the real paused true→false gateway-bounce edge.
- Cluster update still uses the global store `reconnectNonce`; a shared follower relays a restart request to the leader. The per-page fallback retains separate system retry timers and AlertsProvider's independent watchdog and retry state.

## Hook directory

The per-hook data catalog (what each `use-*` hook reads, folds, and refetches)
has its own node: [[ui/web/src/frontend-data-flow/hook-directory.ava.okf.md|Hook directory]].

## Frontend telemetry (user modeling, #1092)

Tracked interactions flow **one-way** to the gateway — never into React Query / SSE:

`lib/telemetry.ts` `track(element, {page, key, value, dedupe})` → in-memory buffer (dedupe 2s per page/element/key; Web Vitals and API timing opt out; under the 100/min per-tab and 200 pending caps) → batched `POST /api/frontend-telemetry` (sendBeacon on hide/pagehide, fetch+keepalive otherwise) → gateway validates (shape 422 / 64KB 413 / per-session 120/min backstop) → one `frontend_interaction` event per interaction (category=telemetry, source=user) → the unified event stream (Loki) → Grafana core panels.

Instrumented points: page views plus native FCP/final LCP/CLS/INP (`lib/telemetry-page-view.tsx` + `lib/web-vitals.ts`, mounted in AuthGuard's authenticated branch), API requests slower than 800ms (`lib/api.ts`, normalized numeric path segments), composer send-to-first-turn-start latency (`lib/interaction-timing.ts`), agent lifecycle actions (`lib/use-agent-actions.ts` onSuccess), message send/stop (`components/composer.tsx`), and every user_settings change (`lib/use-user-settings.ts` setSetting). No sensitive content: `element` is a closed union, keys are bounded normalized identifiers, and `value` is a ≤64-char scalar.
