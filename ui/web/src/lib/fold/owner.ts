// R4 layer 1 — the fold owner (Task #1024).
//
// ONE subscriber owns every domain's snapshot×SSE reconciliation for the
// global broadcast: applyEvent folds system events into the query cache, and
// the connection "open" handler runs the central reconnect reconcile
// (invalidate all queries — events missed during a disconnect gap are
// repaired wholesale). This replaced the per-hook folding skeletons
// (useAgentsCacheSync and friends); hooks only read their keys now.
//
// Debounce policy (the invalidating domains): fleet-graph / tasks bursts
// collapse into one refetch per key family.

"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef } from "react";

import { applyEvent, type FoldContext } from "./index";
import type { SystemEvent } from "../types";
import type { ConnectionEvent } from "../useEventStream";

const INVALIDATE_DEBOUNCE_MS = 2_000;

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

  const ctx: FoldContext = useMemo(
    () => ({
      getQueryData: (key) => queryClient.getQueryData(key),
      setQueryData: (key, value) => queryClient.setQueryData(key, value),
      invalidateQueries: (key) => {
        const family = String(key[0]);
        const existing = timersRef.current.get(family);
        if (existing !== undefined) clearTimeout(existing);
        timersRef.current.set(
          family,
          setTimeout(() => {
            timersRef.current.delete(family);
            void queryClient.invalidateQueries({ queryKey: key });
          }, INVALIDATE_DEBOUNCE_MS),
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

  // Last full-repair timestamp — throttles the invalidate-all below.
  const lastFullInvalidateAtRef = useRef(0);
  // Last logged corrupt frame — dedupe for the parse-failed watch below.
  const lastParseFailedRawRef = useRef<string | null>(null);

  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      // Central reconnect reconcile (moved from useAgentsCacheSync): on a
      // (re)open of the global broadcast, invalidate ALL queries — events
      // pushed while the socket was down were missed, so every cached view
      // could be stale. Throttled to one per RECONNECT_INVALIDATE_WINDOW_MS —
      // reconnect bursts (see the constant) used to each trigger the full
      // 6-8-query refetch storm; a short gap misses few events, and the live
      // stream folds new ones in as they arrive anyway.
      if (ev.type === "open") {
        const now = Date.now();
        if (now - lastFullInvalidateAtRef.current >= RECONNECT_INVALIDATE_WINDOW_MS) {
          lastFullInvalidateAtRef.current = now;
          void queryClient.invalidateQueries();
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
