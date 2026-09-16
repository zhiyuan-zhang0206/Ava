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
  RECONNECT_QUERY_KEYS,
  applyEvent,
  type FoldContext,
} from "./index";
import { createQueryRepairScheduler } from "./repair";
import type { SystemEvent } from "../types";
import type { ConnectionEvent } from "../useEventStream";

export interface FoldOwner {
  onSystemEvent: (ev: SystemEvent) => void;
  onConnectionEvent: (ev: ConnectionEvent) => void;
}

/** Build the fold owner — one per EventStreamProvider. */
export function useFoldOwner(): FoldOwner {
  const queryClient = useQueryClient();
  const repairsRef = useRef<ReturnType<typeof createQueryRepairScheduler> | null>(null);
  useEffect(() => {
    const repairs = createQueryRepairScheduler(queryClient);
    repairsRef.current = repairs;
    return () => {
      repairs.dispose();
      repairsRef.current = null;
    };
  }, [queryClient]);

  const ctx: FoldContext = useMemo(
    () => ({
      getQueryData: (key) => queryClient.getQueryData(key),
      setQueryData: (key, value) => queryClient.setQueryData(key, value),
      invalidateQueries: (key) => repairsRef.current?.request(key),
    }),
    [queryClient],
  );

  const onSystemEvent = useCallback(
    (ev: SystemEvent) => {
      applyEvent(ctx, ev);
    },
    [ctx],
  );

  // Last logged corrupt frame — dedupe for the parse-failed watch below.
  const lastParseFailedRawRef = useRef<string | null>(null);

  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      // Central reconnect reconcile: only cache families owned by the global
      // fold can have missed these events. Invalidating unrelated active
      // queries (settings, config, inspector aggregates, status) multiplied a
      // reconnect into a fleet-wide request storm without repairing anything.
      if (ev.type === "open") {
        for (const queryKey of RECONNECT_QUERY_KEYS) {
          repairsRef.current?.request(queryKey, true);
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
    [],
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
