"use client";

// Cluster-health poller — the update-aware half of the SSE-resilience story.
//
// Why this exists: the single SSE connection can go half-dead exactly when
// the cluster gracefully restarts (an update / rollout / `ava cluster update`). The
// browser may keep the EventSource OPEN against a torn-down server, so
// `onerror` never fires and the watchdog (useEventStream) is the only thing
// that would eventually catch it — at the 45s deadline. This hook closes
// that gap from the other side: it polls the cluster's paused flag, so when
// a rollout finishes (`paused` flips true -> false) we reconnect SSE and
// refetch agents promptly instead of waiting on the watchdog.
//
// The operational poll on `/api/cluster/status` carries every signal this
// hook needs (`paused` + `current_orchestration`), and it is the one status
// path that bypasses the cluster-paused 503 middleware, so it stays readable
// *while* paused — which also lets it distinguish a normal in-flight rollout
// (paused + orchestration set) from a stranded pause (paused + orchestration
// null — a hard-killed rollout left the flag) and drive the stranded-recovery
// banner (AppConnectionBanner). There is deliberately NO app-root observer on
// the heavier /api/status here: its freshness belongs to the routes that
// render it (the sidebar SpawnButton's shared ["status"] poll on Home, the
// visibility-gated observers on Insights Status / Control Config).
//
// Cadence: the edge this loop exists for happens a handful of times a week,
// so the steady state idles at 15s (the SSE watchdog still backstops at 45s)
// and tightens to 5s only while an update is actually in flight — fast
// polling exactly during the window it covers, near-zero cost otherwise.
//
// Mount this once, at the app root (components/app-connection-banner.tsx).

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
// The 503-bypassing local cluster snapshot (paused + current_orchestration),
// readable even while the cluster is paused.
export const CLUSTER_STATUS_QUERY_KEY = ["cluster-status"] as const;

// Steady-state / in-flight poll intervals — see the cadence note above.
const IDLE_POLL_MS = 15_000;
const UPDATING_POLL_MS = 5_000;

/**
 * Poll authenticated cluster status and drive reconnect/recovery state.
 */
export function useClusterHealth(): void {
  const queryClient = useQueryClient();
  const setClusterStranded = useStore((s) => s.setClusterStranded);
  const bumpReconnect = useStore((s) => s.bumpReconnect);

  const { data } = useQuery({
    queryKey: CLUSTER_STATUS_QUERY_KEY,
    queryFn: api.getClusterStatus,
    // Function form: tighten while an update is in flight so the finish edge
    // lands within seconds; idle at 15s otherwise. Failed fetches during the
    // gateway restart window are tolerated — TanStack retries on its own
    // cadence, and a missed poll just defers the edge detection by one
    // interval (the watchdog still backstops).
    refetchInterval: (query) => {
      const snap = query.state.data;
      const updating = snap != null && (snap.paused || snap.current_orchestration != null);
      return updating ? UPDATING_POLL_MS : IDLE_POLL_MS;
    },
  });

  // Stranded = paused with no orchestration alive: a rollout was hard-killed and
  // left the pause + lock behind (a healthy rollout clears both together on
  // finish). Undefined data (initial / fetch fail) is not stranded — never offer
  // recovery without a confirmed snapshot.
  const stranded =
    data != null && data.paused && data.current_orchestration == null;

  useEffect(() => {
    setClusterStranded(stranded);
  }, [stranded, setClusterStranded]);

  const paused = data?.paused ?? false;
  // Edge-detect paused true -> false (update finished). prevPaused starts
  // false, so a first poll that lands while still paused arms the edge; the
  // following poll that sees it clear fires the reconnect. A cold start that
  // is already unpaused never triggers (false -> false).
  const prevPausedRef = useRef(false);
  useEffect(() => {
    const wasPaused = prevPausedRef.current;
    prevPausedRef.current = paused;
    if (wasPaused && !paused) {
      // Rollout/restart just completed — the old SSE socket was severed when
      // the gateway bounced. Reopen it now and reconcile the agents list.
      bumpReconnect();
      void queryClient.refetchQueries({ queryKey: AGENTS_QUERY_KEY });
    }
  }, [paused, bumpReconnect, queryClient]);
}
