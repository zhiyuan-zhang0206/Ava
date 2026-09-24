"use client";

// Profile-shared basic, detail, and alert streams use one visible leader's
// EventSources. Providers retain their per-page fold and subscriber contracts.
// Hidden pages leave the transport and reconcile authoritative reads on open.
// Alerts have a separate domain provider. Frames may contain a single event or
// a batch; batch consumers fold one frame in one state update.

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

import { API_BASE, api } from "./api";
import { notifySessionInvalid, useAuth } from "./auth-context";
import { useFoldOwner } from "./fold/owner";
import { useDocumentVisible } from "./use-document-visible";
import { createSseLifecycle } from "@/lib/sse-lifecycle";
import { reportSseTransportMode, sharedSseSupported, sharedSseTransport, type SseChannel } from "./sse-share";
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
// non-2xx *response* — a 503 while the cluster is paused for a rollout or a 500 —
// it gives up permanently (readyState → CLOSED, onerror fires once and never
// again). A 401/403 stops through the session probe below; transient failures get
// a capped exponential backoff, single-flight (never stack timers), and reset the
// backoff on a successful open.
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
  /** Frame-batch delivery (optional): when present, ONE SSE frame's events
   * arrive in a single call instead of one `system` call per event. The
   * high-rate all-events stream carries the active agent's deltas batched at
   * up to 10 frames/s; a batch subscriber folds a whole frame inside one
   * store update — one notification + one render per frame instead of one
   * per event (the storm that janked the home page while busy agents
   * streamed). Subscribers without it keep the per-event contract. When
   * present, `system` is not called for that subscriber. */
  systemBatch?: (events: SystemEvent[]) => void;
}

interface EventStreamContextValue {
  subscribe: (
    onSystem: (ev: SystemEvent) => void,
    onConnection: (ev: ConnectionEvent) => void,
    onSystemBatch?: (events: SystemEvent[]) => void,
  ) => () => void;
}

/** Fan one shared or legacy SSE channel into the unchanged subscriber API.
 * `onOpenChange` drives the global Provider's e2e-ready marker. */
function useSseConnection(
  url: string | null,
  channel: SseChannel,
  activeId: number | null,
  isVisible: boolean,
  subscribersRef: React.RefObject<Set<Subscriber>>,
  onOpenChange: (open: boolean) => void,
): void {
  const { status: authStatus } = useAuth();
  // A cluster-update reconnect bump re-runs this effect; the leader replaces
  // its source and the subsequent open reconciles missed events.
  const reconnectNonce = useStore((s) => s.reconnectNonce);
  const bumpReconnect = useStore((s) => s.bumpReconnect);
  // Legacy-only CLOSED retry, separate from the global reconnect lever.
  const [retryNonce, setRetryNonce] = useState(0);
  const failCountRef = useRef(0);
  const lastReconnectNonce = useRef(reconnectNonce);
  const shared = sharedSseSupported();

  useEffect(() => {
    if (authStatus !== "authenticated" || !isVisible || (!shared && url === null)) {
      // Not authenticated: keep the connection closed. EventSource cannot set an
      // Authorization header, so an unauthenticated SSE GET 401s at the gateway —
      // opening it would just feed the retry storm (Task #1635). When auth flips
      // to "authenticated" (login), this effect re-runs and opens fresh.
      lastReconnectNonce.current = reconnectNonce;
      onOpenChange(false);
      return;
    }
    reportSseTransportMode();
    // EventSource needs withCredentials for the cross-origin session cookie.
    // Consecutive parse failures within one connection: the backend
    // validates every frame (pydantic rejects raw control characters), so
    // repeated parse errors mean the transport is corrupting frames — count
    // and force a reconnect at 3 (Task #951).
    let parseFailures = 0;
    const transport = shared ? sharedSseTransport() : null;
    let es: EventSource | null = null;
    if (!transport) {
      if (url === null) throw new Error("legacy SSE URL missing");
      es = new EventSource(url, { withCredentials: true });
    }

    const lifecycle = createSseLifecycle({
      failCount: failCountRef,
      watchdogMs: WATCHDOG_MS,
      reconnectBaseMs: RECONNECT_BASE_MS,
      reconnectMaxMs: RECONNECT_MAX_MS,
      onWatchdog: () => {
        for (const sub of subscribersRef.current) sub.conn({ type: "reconnecting" });
        if (transport) transport.restart(channel);
        else bumpReconnect();
      },
      onRetry: () => setRetryNonce((n) => n + 1),
      onClosed: () => {
        for (const sub of subscribersRef.current) sub.conn({ type: "closed" });
      },
      onConnecting: () => {
        for (const sub of subscribersRef.current) sub.conn({ type: "reconnecting" });
      },
      checkAuth: () => api.checkAuth(),
      onInvalidSession: notifySessionInvalid,
    });

    const handleOpen = () => {
      if (lifecycle.isDisposed()) return;
      lifecycle.resetBackoff();
      onOpenChange(true);
      lifecycle.armWatchdog();
      for (const sub of subscribersRef.current) sub.conn({ type: "open" });
    };
    const handleFrame = (raw: string) => {
      if (lifecycle.isDisposed()) return;
      // Any frame = the connection is alive — reset the watchdog before
      // anything else (heartbeat counts too).
      lifecycle.armWatchdog();
      // e.data is spec'd as string, but browser implementations (Blob
      // mode / unusual toString) sometimes drift; defensive cast
      // prevents a String() throw from drowning the catch.

      // Parse here; subscriber exceptions must not count as corrupt frames.
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
        // Three corrupt frames force a backoff reopen (Task #951).
        parseFailures += 1;
        if (parseFailures >= 3) {
          parseFailures = 0;
          if (transport) {
            if (transport.isLeader()) transport.restart(channel, true);
            return;
          }
          if (es === null) throw new Error("legacy EventSource unavailable", { cause: err });
          es.close();
          // Capped backoff, same as a dead-end CLOSED — an immediate reopen
          // against a source that keeps sending corrupt frames reconnects
          // into the same garbage in a tight loop (Task #1051).
          lifecycle.scheduleReopen();
        }
        return;
      }

      // The leader relays the union detail feed. Preserve each tab's old
      // selected-agent stream contract before any subscriber sees a frame.
      if (transport && channel === "systemAll") {
        events = activeId === null ? [] : events.filter((event) =>
          event.agent_id === 0 || event.agent_id === activeId);
        if (events.length === 0) return;
      }

      // Isolate subscriber failures; batch subscribers receive one call per frame.
      for (const sub of subscribersRef.current) {
        try {
          if (sub.systemBatch) {
            sub.systemBatch(events);
          } else {
            for (const event of events) {
              sub.system(event);
            }
          }
        } catch (subErr) {
          console.error("[useSseConnection] subscriber threw while folding an event", subErr);
        }
      }
    };
    if (transport) {
      const unsubscribe = transport.subscribe(channel, {
        onFrame: handleFrame,
        onState: (state) => {
          if (lifecycle.isDisposed()) return;
          if (state === "open") {
            handleOpen();
          } else {
            if (state === "closed") {
              lifecycle.clearWatchdog();
            }
            for (const sub of subscribersRef.current) sub.conn({ type: state });
          }
        },
      }, activeId);
      if (lastReconnectNonce.current !== reconnectNonce) transport.restart(channel);
      lastReconnectNonce.current = reconnectNonce;
      return () => {
        lifecycle.dispose();
        unsubscribe();
      };
    }

    if (es === null) throw new Error("legacy EventSource unavailable");
    es.onopen = handleOpen;
    es.onmessage = (event) => handleFrame(
      typeof event.data === "string" ? event.data : "[non-string SSE payload]",
    );
    es.onerror = () => lifecycle.handleLegacyError(es);

    return () => {
      lifecycle.dispose();
      es.close();
    };
    // url + authStatus + reconnectNonce + retryNonce are the levers (retryNonce is
    // the local connect-failure backoff). subscribersRef / onOpenChange /
    // bumpReconnect are stable identities included only to satisfy the lint.
  }, [url, channel, activeId, isVisible, shared, authStatus, reconnectNonce, retryNonce,
    bumpReconnect, subscribersRef, onOpenChange]);
}

/** Mint a stable `subscribe(onSystem, onConn)` over a subscriber Set. */
function useSubscribe(
  subscribersRef: React.RefObject<Set<Subscriber>>,
): EventStreamContextValue["subscribe"] {
  return useCallback(
    (onSystem, onConnection, onSystemBatch) => {
      const handler: Subscriber = {
        system: onSystem,
        conn: onConnection,
        systemBatch: onSystemBatch,
      };
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
  onSystemBatch?: (events: SystemEvent[]) => void,
): void {
  if (!ctx) {
    throw new Error(`${hookName} must be used inside <${providerName}>`);
  }
  useEffect(() => {
    return ctx.subscribe(onSystemEvent, onConnectionEvent, onSystemBatch);
  }, [ctx, onSystemEvent, onConnectionEvent, onSystemBatch]);
}

// ---------------------------------------------------------------------------
// Global broadcast — cross-agent, low-frequency GLOBAL_ROLES for all agents.
// ---------------------------------------------------------------------------

const EventStreamContext = createContext<EventStreamContextValue | null>(null);

/**
 * Provider for the global `/api/system` broadcast. The profile leader's
 * EventSource serves all visible pages; this page leaves while hidden.
 * Renders an `sse-ready` marker once OPEN — e2e tests
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
  // Reopening reconciles any events missed while the page was hidden.
  const isVisible = useDocumentVisible();

  useSseConnection(
    isVisible ? `${API_BASE}/api/system` : null,
    "system",
    null,
    isVisible,
    subscribersRef,
    setSseOpen,
  );
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
// Active-agent events — full roles, throttled + authenticated-only.
// ---------------------------------------------------------------------------

const AgentEventStreamContext = createContext<EventStreamContextValue | null>(null);

// The all-events connection has no e2e-ready marker (the global Provider's
// sse-ready already gates page interactivity); its OPEN state drives no UI.
const NOOP_OPEN_CHANGE = (_open: boolean): void => undefined;
/** The detailed feed belongs exclusively to the selected, visible agent. */
export function AgentEventStreamProvider({ children }: { children: React.ReactNode }) {
  const subscribersRef = useRef<Set<Subscriber>>(new Set());
  const activeId = useStore((s) => s.activeId);
  const isVisible = useDocumentVisible();
  const url = isVisible && activeId !== null
    ? `${API_BASE}/api/system/all?agents=${activeId}`
    : null;
  useSseConnection(url, "systemAll", activeId, isVisible, subscribersRef, NOOP_OPEN_CHANGE);
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
 * as useEventStream. The stream carries the active agent plus system-level
 * events; consumers still filter internally via isEventForThread / role checks.
 */
export function useAgentEventStream(
  onSystemEvent: (event: SystemEvent) => void,
  onConnectionEvent: (ev: ConnectionEvent) => void,
  onSystemEventBatch?: (events: SystemEvent[]) => void,
): void {
  const ctx = useContext(AgentEventStreamContext);
  useSubscribeEffect(
    ctx,
    "useAgentEventStream",
    "AgentEventStreamProvider",
    onSystemEvent,
    onConnectionEvent,
    onSystemEventBatch,
  );
}
