// Poll readable host status and reconnect after pause clears.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api } from "./api";
import { useStore } from "./store";
import { AGENTS_QUERY_KEY } from "./use-agents";

// The single query key for GET /api/status across the whole app: the sidebar
// SpawnButton poll, the agent-row + fleet-graph machine badges, and the
// settings Status + Config views. One key ⇒ TanStack dedupes co-mounted
// observers into ONE poll loop instead of several independent ones hitting
// the same endpoint (the connection-budget concern). The bare `["status"]`
// literal in those UI components is this same key by value — keep them in
// sync.
export const SYSTEM_STATUS_QUERY_KEY = ["status"] as const;
// The 503-bypassing local cluster snapshot (paused and readiness),
// readable even while the cluster is paused.
export const CLUSTER_STATUS_QUERY_KEY = ["cluster-status"] as const;

// Steady-state / paused poll intervals.
const IDLE_POLL_MS = 15_000;
const PAUSED_POLL_MS = 5_000;

/**
 * Poll authenticated cluster status and drive reconnect state.
 */
export function useClusterHealth(): void {
  const queryClient = useQueryClient();
  const bumpReconnect = useStore((s) => s.bumpReconnect);

  const { data } = useQuery({
    queryKey: CLUSTER_STATUS_QUERY_KEY,
    queryFn: api.getClusterStatus,
    // Function form: tighten while this host reads paused (a pause,
    // maintenance hold or unfinished startup) so the resume edge lands within
    // seconds; idle at 15s otherwise. Failed fetches during a gateway restart
    // are tolerated — TanStack retries on its own cadence, and a missed poll
    // just defers the edge detection by one interval.
    refetchInterval: (query) => {
      const paused = query.state.data?.paused ?? false;
      return paused ? PAUSED_POLL_MS : IDLE_POLL_MS;
    },
  });

  const paused = data?.paused ?? false;
  // Edge-detect paused true -> false (the host resumed). prevPaused starts
  // false, so a first poll that lands while still paused arms the edge; the
  // following poll that sees it clear fires the reconnect. A cold start that
  // is already unpaused never triggers (false -> false).
  const prevPausedRef = useRef(false);
  useEffect(() => {
    const wasPaused = prevPausedRef.current;
    prevPausedRef.current = paused;
    if (wasPaused && !paused) {
      // The host resumed — an SSE socket may have been severed while the
      // gateway was paused or restarting. Reopen it and reconcile the agents list.
      bumpReconnect();
      void queryClient.refetchQueries({ queryKey: AGENTS_QUERY_KEY });
    }
  }, [paused, bumpReconnect, queryClient]);
}
