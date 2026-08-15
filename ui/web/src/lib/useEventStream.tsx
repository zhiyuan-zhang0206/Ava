"use client";

// Three SSE connections, unified under two Providers:
//
// - EventStreamProvider / useEventStream: the GLOBAL broadcast
//   (`/api/system`). The server forwards only GLOBAL_ROLES — the
//   cross-agent, low-frequency lifecycle (agent spawned/updated/label,
//   page open/close, notice_posted/notice_resolved) for *every* agent. The
//   sidebar agent list (useAgentsCacheSync, the single root cache writer), the
//   inspector's open-pages list (useAgentPages, folding page_opened/page_closed
//   into its ["agent-pages", agentId] cache), and the inbox feed
//   (useInboxFeed, invalidate-refetch on notice_posted/notice_resolved)
//   subscribe here.
//
// - AgentEventStreamProvider / useAgentEventStream: the ALL-EVENTS
//   throttled broadcast (`/api/system/all`). The server pushes EVERY
//   event for EVERY agent in a batched, throttled stream (max 25
//   pushes/sec, each push a JSON array of events). No agent_id or role
//   filtering server-side — the frontend filters internally via
//   isEventForThread. The timeline (useTimeline), token usage
//   (useTokenUsage), and pending strip (usePendingMessages) subscribe
//   here. Always connected (not tied to activeId).
//
// The batched format (`data: [{...}, {...}]`) is handled transparently:
// the onmessage handler detects arrays and fans out each element as an
// individual SystemEvent to subscribers.
//
// Each Provider holds its connection and fans frames out to its own
// subscriber Set; `useEventStream` / `useAgentEventStream` register a
// callback pair. Using multiple hooks against one Provider shares the one
// EventSource (vs. the old `useEventStream(agentId, ...)` that opened a new
// EventSource per hook call — N parallel connections with independent state
// lifecycles producing strange intermediate banners). The shared machinery
// (`useSseConnection`) is identical for both; only the URL differs.
//
// Why connect directly to FastAPI instead of going through Next
// rewrites: the Turbopack dev proxy buffers SSE.

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

import { API_BASE } from "./api";
import { useFoldOwner } from "./fold/owner";
import { useStore } from "./store";
import type { SystemEvent } from "./types";

// Half-dead-connection watchdog window. The server emits a heartbeat data
// frame after ~15s of silence (so `onmessage` sees liveness even when no
// business events flow), so under a healthy connection a frame arrives at
// least every ~15s. 45s with NO frame at all (not even a heartbeat) means
// the socket is wedged: a graceful server restart or a proxy hop that
// stayed OPEN but stopped delivering. EventSource's own onerror can't see
// this (readyState stays OPEN), so we detect it ourselves and force a
// clean reopen via bumpReconnect().
const WATCHDOG_MS = 45_000;

// SSE-connect-failure retry. EventSource auto-retries a *transient* network
// drop on its own (readyState → CONNECTING, onerror fires repeatedly), but on a
// non-2xx *response* — a 503 while the cluster is paused for a rollout, a 500, an
// auth 401 — it gives up permanently (readyState → CLOSED, onerror fires once and
// never again). That is a dead end: live updates never come back until a manual
// page refresh. So on a CLOSED error we schedule our own reopen with capped
// exponential backoff, single-flight (never stack timers), and reset the backoff
// on a successful open. Invariant: as long as the page is alive, SSE reconnects.
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

/** Connection / parsing-side events (distinct from SystemEvent
 *  business events). Subscribers use these to decide whether to show a
 *  reconnect banner / report schema drift / show a refresh hint, etc. */
export type ConnectionEvent =
  | { type: "open" }
  | { type: "reconnecting" } // readyState=CONNECTING — UA reconnecting per spec
  | { type: "closed" } // server closed (404/500 etc), EventSource no longer retries
  | { type: "parse-failed"; raw: string; error: unknown };

interface Subscriber {
  system: (ev: SystemEvent) => void;
  conn: (ev: ConnectionEvent) => void;
}

interface EventStreamContextValue {
  subscribe: (
    onSystem: (ev: SystemEvent) => void,
    onConnection: (ev: ConnectionEvent) => void,
  ) => () => void;
}

/**
 * Shared connection machinery for both Providers. Holds one EventSource for
 * `url` and fans frames out to `subscribersRef`; `url === null` means "no
 * target" (e.g. no active agent selected) — close any open connection and
 * don't reopen. `onOpenChange(true|false)` mirrors the OPEN state out (the
 * global Provider renders an e2e-ready marker from it; the all-events
 * Provider passes a noop). Re-runs when `url` changes or `reconnectNonce`
 * bumps (watchdog / cluster-update reconnect lever).
 */
function useSseConnection(
  url: string | null,
  subscribersRef: React.RefObject<Set<Subscriber>>,
  onOpenChange: (open: boolean) => void,
): void {
  // reconnectNonce: bumping it (from the watchdog below, or from the
  // cluster-update-done detector in use-cluster-health) re-runs this
  // effect — cleanup closes the stale EventSource, the re-run opens a
  // fresh one (whose onopen fires onConnectionEvent({type:"open"}),
  // which useAgentsCacheSync already turns into an agents refetch to reconcile).
  const reconnectNonce = useStore((s) => s.reconnectNonce);
  const bumpReconnect = useStore((s) => s.bumpReconnect);
  // Local retry lever for connect-failure backoff. Kept separate from the global
  // reconnectNonce so one connection retrying does not re-key the other Provider's
  // EventSource. Consecutive-failure count (for the backoff delay) lives in a ref
  // so it survives the effect re-runs a retry triggers; it resets to 0 on open.
  const [retryNonce, setRetryNonce] = useState(0);
  const failCountRef = useRef(0);

  useEffect(() => {
    if (url === null) {
      onOpenChange(false);
      return;
    }
    // The gateway requires auth (session cookie or Bearer token). EventSource
    // cannot set an Authorization header, so it relies on the session cookie —
    // but a cross-origin EventSource (frontend :3000 -> gateway :8000) only
    // sends cookies when opened with `withCredentials`. Without it the SSE GET
    // arrives uncredentialed, the auth middleware 401s it, and live updates
    // never connect. Mirror the `credentials: "include"` the fetch wrapper in
    // api.ts uses; the gateway answers the credentialed CORS request with
    // `Access-Control-Allow-Origin: <origin>` + `Allow-Credentials: true`.
    // Consecutive parse failures within one connection: the backend
    // validates every frame (pydantic rejects raw control characters), so
    // repeated parse errors mean the transport is corrupting frames — count
    // and force a reconnect at 3 (Task #951).
    let parseFailures = 0;
    const es = new EventSource(url, { withCredentials: true });

    // Half-dead-connection watchdog. Any frame (open / business event /
    // heartbeat) is liveness and resets it. If it ever fires, the socket
    // is wedged at the OPEN level (onerror never told us): tell subscribers
    // we're reconnecting (banner shows) and force a clean reopen.
    let watchdog: ReturnType<typeof setTimeout> | null = null;
    const armWatchdog = () => {
      if (watchdog !== null) clearTimeout(watchdog);
      watchdog = setTimeout(() => {
        for (const sub of subscribersRef.current) sub.conn({ type: "reconnecting" });
        bumpReconnect();
      }, WATCHDOG_MS);
    };

    // Capped-backoff reopen after a dead-end CLOSED error (below). Single-flight:
    // one pending timer at a time — a second CLOSED before it fires is ignored, so
    // a burst never becomes a reconnect storm. Bumping retryNonce re-runs this
    // effect; cleanup closes the stale (already-CLOSED) EventSource and a fresh one
    // opens.
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    const scheduleReopen = () => {
      if (retryTimer !== null) return;
      const delay = Math.min(RECONNECT_BASE_MS * 2 ** failCountRef.current, RECONNECT_MAX_MS);
      failCountRef.current += 1;
      retryTimer = setTimeout(() => {
        retryTimer = null;
        setRetryNonce((n) => n + 1);
      }, delay);
    };

    es.onopen = () => {
      failCountRef.current = 0;
      onOpenChange(true);
      armWatchdog();
      for (const sub of subscribersRef.current) sub.conn({ type: "open" });
    };
    es.onmessage = (e) => {
      // Any frame = the connection is alive — reset the watchdog before
      // anything else (heartbeat counts too).
      armWatchdog();
      // e.data is spec'd as string, but browser implementations (Blob
      // mode / unusual toString) sometimes drift; defensive cast
      // prevents a String() throw from drowning the catch.
      const raw = typeof e.data === "string" ? e.data : "[non-string SSE payload]";

      // Parse ONLY inside the try. The subscriber fan-out lives OUTSIDE it:
      // a subscriber throwing used to be caught here, miscounted as a parse
      // failure (3 of those force a reconnect, Task #951) and cut the rest
      // of the batch short — one buggy hook could kill the stream for every
      // subscriber. A corrupt frame is a transport problem; a throwing
      // subscriber is a code problem, isolated per-subscriber below.
      // (No initializer: the catch below returns, so on the fan-out path
      // events is always assigned by the try.)
      let events: SystemEvent[] | null;
      try {
        const parsed = JSON.parse(raw) as unknown;
        // Heartbeat is an ad-hoc liveness frame (`{"role":"heartbeat"}`),
        // not part of the SystemEvent union — check the role string before
        // casting, and never fan it out as a business event. The watchdog
        // reset above already consumed its only meaning (liveness).
        if (
          typeof parsed === "object" &&
          parsed !== null &&
          !Array.isArray(parsed) &&
          (parsed as { role?: unknown }).role === "heartbeat"
        ) {
          return;
        }
        if (Array.isArray(parsed)) {
          // Batched format from /api/system/all: `data: [{...}, {...}]`.
          // Drop non-object elements (corrupt batch members) instead of
          // as-casting them into SystemEvents.
          const list: SystemEvent[] = [];
          for (const item of parsed) {
            if (typeof item !== "object" || item === null) continue;
            if ((item as { role?: unknown }).role === "heartbeat") continue;
            list.push(item as SystemEvent);
          }
          events = list;
        } else {
          events = [parsed as SystemEvent];
        }
      } catch (err) {
        for (const sub of subscribersRef.current) {
          sub.conn({ type: "parse-failed", raw, error: err });
        }
        // Repeated unparseable frames = corrupt transport (the backend
        // validates every event before sending — pydantic rejects raw
        // control characters, Task #951). Force a reconnect at 3 instead
        // of soldiering on with a half-open stream that keeps delivering
        // garbage.
        parseFailures += 1;
        if (parseFailures >= 3) {
          parseFailures = 0;
          es.close();
          // Capped backoff, same as a dead-end CLOSED — an immediate reopen
          // against a source that keeps sending corrupt frames reconnects
          // into the same garbage in a tight loop (Task #1051).
          scheduleReopen();
        }
        return;
      }

      // Fan-out (outside the try, per-subscriber isolated): one subscriber's
      // bug must not starve the others of this frame or of future frames.
      for (const event of events) {
        for (const sub of subscribersRef.current) {
          try {
            sub.system(event);
          } catch (subErr) {
            console.error("[useSseConnection] subscriber threw while folding an event", subErr);
          }
        }
      }
    };
    es.onerror = () => {
      // Explicit tri-state dispatch (CONNECTING/OPEN/CLOSED), no
      // case _: catch-all. Treating a transient OPEN-state error
      // (common on Safari mobile under flaky network) as reconnecting
      // would make the amber banner flicker and hurt UX.
      switch (es.readyState) {
        case EventSource.CLOSED:
          // Dead end: the browser will NOT retry a CLOSED EventSource. Tell
          // subscribers, then schedule our own backoff reopen so the connection
          // comes back on its own once the server is answering again.
          for (const sub of subscribersRef.current) sub.conn({ type: "closed" });
          scheduleReopen();
          return;
        case EventSource.CONNECTING:
          // The browser is already auto-retrying — surface it, but do NOT also
          // schedule a manual reopen (that would double up into a storm).
          for (const sub of subscribersRef.current) {
            sub.conn({ type: "reconnecting" });
          }
          return;
        case EventSource.OPEN:
          // Socket still alive; transient fault absorbed by the browser; don't notify UI
          return;
        default:
          throw new Error(`unknown EventSource readyState: ${es.readyState}`);
      }
    };

    return () => {
      if (watchdog !== null) clearTimeout(watchdog);
      if (retryTimer !== null) clearTimeout(retryTimer);
      es.close();
    };
    // url + reconnectNonce + retryNonce are the levers (retryNonce is the local
    // connect-failure backoff). subscribersRef / onOpenChange / bumpReconnect are
    // stable identities included only to satisfy the lint.
  }, [url, reconnectNonce, retryNonce, bumpReconnect, subscribersRef, onOpenChange]);
}

/** Mint a stable `subscribe(onSystem, onConn)` over a subscriber Set. */
function useSubscribe(
  subscribersRef: React.RefObject<Set<Subscriber>>,
): EventStreamContextValue["subscribe"] {
  return useCallback(
    (onSystem, onConnection) => {
      const handler: Subscriber = { system: onSystem, conn: onConnection };
      subscribersRef.current.add(handler);
      return () => {
        subscribersRef.current.delete(handler);
      };
    },
    [subscribersRef],
  );
}

function useSubscribeEffect(
  ctx: EventStreamContextValue | null,
  hookName: string,
  providerName: string,
  onSystemEvent: (event: SystemEvent) => void,
  onConnectionEvent: (ev: ConnectionEvent) => void,
): void {
  if (!ctx) {
    throw new Error(`${hookName} must be used inside <${providerName}>`);
  }
  useEffect(() => {
    return ctx.subscribe(onSystemEvent, onConnectionEvent);
  }, [ctx, onSystemEvent, onConnectionEvent]);
}

// ---------------------------------------------------------------------------
// Global broadcast — cross-agent, low-frequency GLOBAL_ROLES for all agents.
// ---------------------------------------------------------------------------

const EventStreamContext = createContext<EventStreamContextValue | null>(null);

/**
 * Provider for the global `/api/system` broadcast. One EventSource serves
 * all subscribers; it never closes on agent switch (the broadcast is
 * agent-agnostic). Renders an `sse-ready` marker once OPEN — e2e tests
 * (`page.wait_for_selector('[data-testid="sse-ready"]')`) gate on it before
 * interacting, so SSE-driven UI isn't raced.
 */
export function EventStreamProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  // Mutable Set of subscribers — useRef avoids re-renders on every subscribe.
  const subscribersRef = useRef<Set<Subscriber>>(new Set());
  const [sseOpen, setSseOpen] = useState(false);

  useSseConnection(`${API_BASE}/api/system`, subscribersRef, setSseOpen);
  const subscribe = useSubscribe(subscribersRef);

  // ── The fold (R4 layer 1): ONE subscriber owns every domain's snapshot×SSE
  // reconciliation — applyEvent folds system events into the query cache, and
  // the connection "open" handler runs the central reconnect reconcile
  // (invalidate all queries — events missed during a disconnect gap are
  // repaired wholesale). This replaced the per-hook folding skeletons
  // (useAgentsCacheSync and friends); hooks only read their keys now.
  const fold = useFoldOwner();

  // Subscribe the fold — it outlives every per-page subscriber (the connection
  // persists across navigation), so no lifecycle event is dropped while no page
  // reader is mounted. Standard effect: on dependency change React runs the
  // cleanup (unsubscribe) BEFORE the new setup (subscribe), so a re-render can
  // never leave the fold unsubscribed. The ref-guarded "subscribe once" variant
  // did exactly that — after the first cleanup it early-returned and the fold
  // stayed unsubscribed forever (Task #1033). useFoldOwner returns a stable
  // object, so in practice this effect runs once.
  useEffect(() => {
    return subscribe(fold.onSystemEvent, fold.onConnectionEvent);
  }, [subscribe, fold]);

  // value reference is stable (subscribe is from useCallback) — consumers
  // using useEffect deps don't trigger unnecessary unsubscribe/resubscribe
  // on Provider re-render.
  return (
    <EventStreamContext.Provider value={{ subscribe }}>
      {children}
      {sseOpen ? <div data-testid="sse-ready" className="hidden" aria-hidden /> : null}
    </EventStreamContext.Provider>
  );
}


/**
 * Subscribe to the global broadcast. Must be called inside
 * `<EventStreamProvider>`.
 *
 * `onConnectionEvent` is required — do not let silent failure sneak
 * back through a default fallback. Callers that don't care about
 * connection state must still pass an explicit noop with a comment
 * explaining why.
 */
export function useEventStream(
  onSystemEvent: (event: SystemEvent) => void,
  onConnectionEvent: (ev: ConnectionEvent) => void,
): void {
  const ctx = useContext(EventStreamContext);
  useSubscribeEffect(ctx, "useEventStream", "EventStreamProvider", onSystemEvent, onConnectionEvent);
}

// ---------------------------------------------------------------------------
// All-events — full SYSTEM_ROLES for EVERY agent, throttled + always connected.
// ---------------------------------------------------------------------------

const AgentEventStreamContext = createContext<EventStreamContextValue | null>(null);

// The all-events connection has no e2e-ready marker (the global Provider's
// sse-ready already gates page interactivity); its OPEN state drives no UI.
const NOOP_OPEN_CHANGE = (_open: boolean): void => undefined;

/**
 * Provider for the all-events throttled broadcast (`/api/system/all`).
 * Always connected — not tied to `activeId`. The server pushes EVERY
 * event for EVERY agent in a batched, throttled stream (max 25
 * pushes/sec). No server-side filtering — the frontend's per-consumer
 * `isEventForThread` guard handles agent routing.
 */
export function AgentEventStreamProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const subscribersRef = useRef<Set<Subscriber>>(new Set());

  // Always connected to the all-events broadcast — no dependency on activeId.
  useSseConnection(`${API_BASE}/api/system/all`, subscribersRef, NOOP_OPEN_CHANGE);
  const subscribe = useSubscribe(subscribersRef);

  return (
    <AgentEventStreamContext.Provider value={{ subscribe }}>
      {children}
    </AgentEventStreamContext.Provider>
  );
}

/**
 * Subscribe to the all-events throttled broadcast. Must be called inside
 * `<AgentEventStreamProvider>`. Same `onConnectionEvent`-required contract
 * as useEventStream. The stream carries EVERY event for EVERY agent —
 * consumers filter internally via isEventForThread / role checks.
 */
export function useAgentEventStream(
  onSystemEvent: (event: SystemEvent) => void,
  onConnectionEvent: (ev: ConnectionEvent) => void,
): void {
  const ctx = useContext(AgentEventStreamContext);
  useSubscribeEffect(
    ctx,
    "useAgentEventStream",
    "AgentEventStreamProvider",
    onSystemEvent,
    onConnectionEvent,
  );
}
