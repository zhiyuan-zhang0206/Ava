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
//
// One map gap is presentation-only: during an external takeover an inbound is
// recorded into the permanent timeline at insert time (capture trigger) while
// its row stays status='pending' until the executor ACKs — the same message
// would render in both surfaces (task #3683). `withoutTimelineDuplicates`
// drops those twins at the render site; the server list stays authoritative.

"use client";

import { useQuery } from "@tanstack/react-query";
import { useCallback, useRef } from "react";

import { api } from "./api";
import { useAgentReconcile } from "./agent-reconcile";
import { useAgentReadRepair } from "./use-agent-read-repair";
import { CONVERSATION_RETENTION_MS } from "./switch-budget";
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
//    R11 storm; collapse the burst into a coalesced read with a fixed deadline instead.
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

/**
 * Drop pending inbounds already visible in the timeline (task #3683).
 *
 * During an external takeover the capture trigger records an inbound into the
 * permanent timeline at insert time while its row is still status='pending'
 * (it clears only at the executor's ACK), so the strip would otherwise show a
 * message that is already in the conversation. The conversation wins.
 */
export function withoutTimelineDuplicates(
  pending: readonly PendingInbound[],
  timelineItems: readonly { readonly inbound_id: number | null }[],
): PendingInbound[] {
  const inTimeline = new Set<number>();
  for (const item of timelineItems) {
    if (item.inbound_id !== null) inTimeline.add(item.inbound_id);
  }
  return pending.filter((p) => !inTimeline.has(p.id));
}

export function usePendingMessages(
  agentId: number | null,
  showError: (msg: string) => void,
): PendingInbound[] {
  const { isVisible, requestRepair } = useAgentReadRepair("pending", agentId);
  const { requestReconcile } = useAgentReconcile(agentId);
  const seenParseErrors = useRef<Set<string>>(new Set());

  const query = useQuery({
    queryKey: ["pending", agentId] as const,
    queryFn: ({ signal }) => {
      if (agentId === null) throw new Error("A pending read requires an agent");
      return api.getPendingMessages(agentId, signal);
    },
    enabled: agentId !== null && isVisible,
    // A retained queue is also fresh (task #3900 batch 2): a switch back
    // shows it immediately and fires no read of its own — RQ auto-reads a
    // key switch only when the target key is stale. The one refresh is the
    // re-attach reconcile (agent-reconcile.ts); the queue's own event
    // invalidations (inbound/turn roles below) still refetch explicitly.
    staleTime: CONVERSATION_RETENTION_MS,
    gcTime: CONVERSATION_RETENTION_MS,
    refetchOnMount: false,
  });

  const onEvent = useCallback(
    (ev: SystemEvent) => {
      if (agentId == null || !isEventForThread(ev, agentId)) return;
      // A fresh inbound / a commit changes the set once and is low-frequency —
      // refetch now so the strip reflects it without lag.
      if (IMMEDIATE_REFETCH_ROLES.has(ev.role)) {
        requestRepair();
        return;
      }
      // Repeated turn starts share a fixed repair deadline. A start during
      // the read leaves one trailing repair instead of starving the strip.
      if (TURN_START_ROLES.has(ev.role)) requestRepair(false);
    },
    [agentId, requestRepair],
  );

  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      // Banner/closed states are owned by useTimeline on the same shared
      // connection; reopening refreshes the REST snapshot, and parse failures are
      // surfaced (deduped) so schema drift doesn't fail silently.
      switch (ev.type) {
        case "open":
          // The shared composed reconcile refreshes the queue (and the other
          // two conversation models) with one request on re-attach.
          requestReconcile();
          return;
        case "parse-failed": {
          const key = String(ev.error);
          if (seenParseErrors.current.has(key)) return;
          seenParseErrors.current.add(key);
          showError(`Pending SSE parse failed: ${key}`);
          return;
        }
        case "reconnecting":
        case "closed":
          return;
      }
    },
    [requestReconcile, showError],
  );

  useAgentEventStream(onEvent, onConnectionEvent);

  return query.data ?? [];
}
