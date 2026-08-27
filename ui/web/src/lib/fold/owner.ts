// R4 layer 1 — the fold owner (Task #1024).
//
// ONE subscriber owns every domain's snapshot×SSE reconciliation for the
// global broadcast: applyEvent folds system events into the query cache, and
// the connection "open" handler runs the central reconnect reconcile
// (invalidate fold-owned query families — events missed during a disconnect
// gap are repaired without refetching unrelated caches). This replaced the
// per-hook folding skeletons
// (useAgentsCacheSync and friends); hooks only read their keys now.
//
// Debounce policy (the invalidating domains): fleet-graph / tasks bursts
// collapse into one refetch per key family.

"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef } from "react";

import {
  AGENTS_QUERY_KEY,
  RECONNECT_QUERY_KEYS,
  TERMINATED_AGENTS_QUERY_KEY,
  applyEvent,
  type FoldContext,
} from "./index";
import type { SystemEvent } from "../types";
import type { ConnectionEvent } from "../useEventStream";

const INVALIDATE_DEBOUNCE_MS = 2_000;
// The notices families use a SHORT window instead of the 2s one (Task #1814):
// a resolve must reflect promptly, but a batch resolve publishes N
// notice_resolved events — immediate invalidation would fan out into up to N
// refetches of the open queue + history. 300ms collapses the burst to one
// refetch per window; the Inbox's optimistic cache drop keeps the click-to-
// row-gone latency at response time regardless.
const NOTICES_INVALIDATE_DEBOUNCE_MS = 300;

// Throttle window for the central reconnect reconcile (the invalidate-all
// below). "open" fires on the initial connect AND on every reconnect — and
// reconnects cluster under flaky networks (mobile Safari CONNECTING/OPEN
// jitter, the 45s watchdog force-reopen — which bumps the shared
// reconnectNonce and re-keys BOTH SSE providers at once — and CLOSED
// backoff retries). Every open used to refetch every active query (~6-8
// GETs × 2 providers when both reopen together). A short gap misses few
// events (the live stream folds them as they arrive), so one full repair
// per window bounds the storm without skipping genuine repairs after a
// long disconnect.
const RECONNECT_INVALIDATE_WINDOW_MS = 30_000;

export interface FoldOwner {
  onSystemEvent: (ev: SystemEvent) => void;
  onConnectionEvent: (ev: ConnectionEvent) => void;
}

/** Build the fold owner — one per EventStreamProvider. */
export function useFoldOwner(): FoldOwner {
  const queryClient = useQueryClient();
  // Per-key-family debounce timers (fleet-graph / tasks), cleared on unmount.
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  useEffect(
    () => () => {
      for (const timer of timersRef.current.values()) clearTimeout(timer);
      timersRef.current.clear();
    },
    [],
  );

  // The notices families use the SHORT debounce window (Task #1814): the open
  // queue and its resolved history are the actively-viewed list — a resolve
  // must reflect promptly, and the refetch is a cheap keyset query. The 2s
  // window remains for the heavy server-computed families (fleet-graph,
  // tasks), whose bursts would otherwise fan out.
  const ctx: FoldContext = useMemo(
    () => ({
      getQueryData: (key) => queryClient.getQueryData(key),
      setQueryData: (key, value) => queryClient.setQueryData(key, value),
      invalidateQueries: (key) => {
        const family = String(key[0]);
        const debounceMs =
          family === "notices" || family === "notices-resolved"
            ? NOTICES_INVALIDATE_DEBOUNCE_MS
            : INVALIDATE_DEBOUNCE_MS;
        const existing = timersRef.current.get(family);
        if (existing !== undefined) clearTimeout(existing);
        timersRef.current.set(
          family,
          setTimeout(() => {
            timersRef.current.delete(family);
            void queryClient.invalidateQueries({ queryKey: key });
          }, debounceMs),
        );
      },
    }),
    [queryClient],
  );

  const onSystemEvent = useCallback(
    (ev: SystemEvent) => {
      applyEvent(ctx, ev);
    },
    [ctx],
  );

  // Last reconnect-repair timestamp — throttles the scoped invalidations below.
  const lastFullInvalidateAtRef = useRef(0);
  // Last logged corrupt frame — dedupe for the parse-failed watch below.
  const lastParseFailedRawRef = useRef<string | null>(null);

  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      // Central reconnect reconcile: only cache families owned by the global
      // fold can have missed these events. Invalidating unrelated active
      // queries (settings, config, inspector aggregates, status) multiplied a
      // reconnect into a fleet-wide request storm without repairing anything.
      if (ev.type === "open") {
        const now = Date.now();
        if (now - lastFullInvalidateAtRef.current >= RECONNECT_INVALIDATE_WINDOW_MS) {
          lastFullInvalidateAtRef.current = now;
          for (const queryKey of RECONNECT_QUERY_KEYS) {
            const exact =
              queryKey === AGENTS_QUERY_KEY ||
              queryKey === TERMINATED_AGENTS_QUERY_KEY;
            void queryClient.invalidateQueries({ queryKey, exact });
          }
        }
      } else if (ev.type === "parse-failed") {
        // The global stream's parse-failed had NO consumer (the all-events
        // consumers each dedupe-toast their own). The stream layer already
        // force-reconnects after 3 consecutive failures (Task #951), so this
        // is a watch signal, not a repair trigger: log each distinct corrupt
        // frame once — repeats of the same payload are the same failure, not
        // new information.
        if (ev.raw !== lastParseFailedRawRef.current) {
          lastParseFailedRawRef.current = ev.raw;
          console.error("[fold] global broadcast parse-failed", ev.raw, ev.error);
        }
      }
    },
    [queryClient],
  );

  // Stable identity: the Provider subscribes the fold in an effect whose deps
  // include this object. An unstable reference re-runs that effect on every
  // Provider render — and the ref-guarded "subscribe once" cleanup then
  // UNSUBSCRIBES without resubscribing (the Fable P0 regression, Task #1033):
  // the first onopen (setSseOpen(true) re-render) permanently killed the fold
  // subscriber and every domain's realtime layer went silently stale. The
  // callbacks are useCallback-stable, so memoizing the pair makes the object
  // stable for the Provider's lifetime.
  return useMemo(() => ({ onSystemEvent, onConnectionEvent }), [onSystemEvent, onConnectionEvent]);
}
