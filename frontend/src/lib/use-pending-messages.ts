// usePendingMessages — the queue of chat inbounds the agent has not
// claimed yet (status='pending'), shown as a compact strip above the
// composer.
//
// Source of truth is the server: GET /api/agents/{id}/pending returns only
// status='pending' rows. We never hold a hand-rolled local list — instead
// we refetch on the SSE events that change the queue (a new inbound was
// enqueued, or the agent started a turn / committed, which claims the
// pending batch). Because the GET filters status='pending' server-side, a
// claimed/committed message simply drops out on the next refetch. This
// deliberately avoids client-side add/remove/dedup bookkeeping (the class
// of races that makes optimistic timeline updates fragile).

"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";

import { api } from "./api";
import { isEventForThread } from "./timeline";
import type { PendingInbound, SystemEvent } from "./types";
import type { ConnectionEvent } from "./useEventStream";
import { useAgentEventStream } from "./useEventStream";

// Roles that change the pending set: inbound_arrived adds one; a turn
// starting (the agent claims the whole pending batch) or inbound_committed
// removes them (they leave status='pending' and show in the timeline).
//
// Two tiers by frequency:
//  - inbound_arrived / inbound_committed each change the set by one and fire at
//    most once per message → refetch immediately so the strip stays responsive.
//  - the *_start roles fire repeatedly across a multi-step turn, yet the batch
//    is claimed exactly once (at the first start). Refetching per start was the
//    R11 storm; collapse the burst into a single debounced refetch instead.
const IMMEDIATE_REFETCH_ROLES: ReadonlySet<SystemEvent["role"]> = new Set([
  "inbound_arrived",
  "inbound_committed",
]);
const TURN_START_ROLES: ReadonlySet<SystemEvent["role"]> = new Set([
  "chat_start",
  "code_start",
  "reasoning_start",
  "exec_start",
]);

export function usePendingMessages(
  agentId: number | null,
  showError: (msg: string) => void,
): PendingInbound[] {
  const queryClient = useQueryClient();
  const seenParseErrors = useRef<Set<string>>(new Set());
  const refetchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const query = useQuery({
    queryKey: ["pending", agentId] as const,
    // eslint-disable-next-line @typescript-eslint/no-non-null-assertion -- the `enabled` gate below guarantees agentId is set before queryFn runs (standard TanStack idiom the types cannot see)
    queryFn: () => api.getPendingMessages(agentId!),
    enabled: agentId != null,
    staleTime: 0,
    // gcTime 30min: keep inactive thread cache so returning from another page
    // hot-hits instead of cold-fetching (matches use-timeline / use-token-usage).
    gcTime: 30 * 60_000,
  });

  const onEvent = useCallback(
    (ev: SystemEvent) => {
      if (agentId == null || !isEventForThread(ev, agentId)) return;
      // A fresh inbound / a commit changes the set once and is low-frequency —
      // refetch now so the strip reflects it without lag.
      if (IMMEDIATE_REFETCH_ROLES.has(ev.role)) {
        void queryClient.invalidateQueries({ queryKey: ["pending", agentId] });
        return;
      }
      // A multi-step turn fires many *_start events, but the batch is claimed
      // once. Collapse the burst into ONE refetch on the trailing edge (2s,
      // matching use-fleet-graph / use-tasks) instead of a GET /pending per
      // start (R11 storm).
      if (!TURN_START_ROLES.has(ev.role)) return;
      if (refetchTimer.current !== null) clearTimeout(refetchTimer.current);
      refetchTimer.current = setTimeout(() => {
        refetchTimer.current = null;
        void queryClient.invalidateQueries({ queryKey: ["pending", agentId] });
      }, 2_000);
    },
    [agentId, queryClient],
  );

  // Cancel a pending debounce when the thread changes / on unmount, so a timer
  // scheduled for the old agent never fires a stray refetch after the switch.
  useEffect(() => {
    return () => {
      if (refetchTimer.current !== null) {
        clearTimeout(refetchTimer.current);
        refetchTimer.current = null;
      }
    };
  }, [agentId]);

  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      // Banner/closed states are owned by useTimeline on the same shared
      // connection; only surface parse failures (deduped) so schema drift
      // on these events doesn't fail silently.
      if (ev.type !== "parse-failed") return;
      const key = String(ev.error);
      if (seenParseErrors.current.has(key)) return;
      seenParseErrors.current.add(key);
      showError(`Pending SSE parse failed: ${key}`);
    },
    [showError],
  );

  useAgentEventStream(onEvent, onConnectionEvent);

  return query.data ?? [];
}
