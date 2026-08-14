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
// refetch agents *immediately* instead of waiting on the watchdog.
//
// It also feeds `clusterUpdating` into the store so AuthGuard can tell an
// expected update-induced auth failure apart from a real logged-out state and
// show the full-screen UpdatingPage instead of redirecting to /login (see
// components/auth/auth-guard.tsx).
//
// A second poll hits `/api/cluster/status` — the one status path that bypasses
// the cluster-paused 503 middleware, so it is readable *while* paused. That lets
// us distinguish a normal in-flight rollout (paused + orchestration set) from a
// stranded pause (paused + orchestration null — a hard-killed rollout left the
// flag), and drive the stranded-recovery banner's affordance for the latter
// (AppConnectionBanner). `/api/status` itself 503s during a pause, so it
// cannot tell these apart on its own.
//
// Mount this once, at the app root (components/app-connection-banner.tsx).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api } from "./api";
import { useStore } from "./store";
import { AGENTS_QUERY_KEY } from "./use-agents";

// The single query key for GET /api/status across the whole app: this hook's
// 5s poll, the sidebar SpawnButton (also 5s), the agent-row + fleet-graph
// machine badges, and the settings Status + Config views. One key ⇒ TanStack
// dedupes them into ONE poll loop instead of two independent ones hitting the
// same endpoint (the connection-budget concern). The bare `["status"]` literal
// in those UI components is this same key by value — keep them in sync.
export const SYSTEM_STATUS_QUERY_KEY = ["status"] as const;
// The 503-bypassing local cluster snapshot (paused + current_orchestration),
// readable even while the cluster is paused.
export const CLUSTER_STATUS_QUERY_KEY = ["cluster-status"] as const;

// 5s poll — fast enough to catch the end of a rollout within a few seconds
// of the gateway coming back, cheap enough to leave running continuously.
const POLL_MS = 5_000;

/**
 * Poll cluster status, mirror `paused` into the store, and on the
 * paused true -> false edge (a rollout/restart just finished) reconnect SSE
 * + refetch agents. Call once at the app root.
 */
export function useClusterHealth(): void {
  const queryClient = useQueryClient();
  const setClusterUpdating = useStore((s) => s.setClusterUpdating);
  const setClusterStranded = useStore((s) => s.setClusterStranded);
  const bumpReconnect = useStore((s) => s.bumpReconnect);

  const { data } = useQuery({
    queryKey: SYSTEM_STATUS_QUERY_KEY,
    queryFn: api.getSystemStatus,
    refetchInterval: POLL_MS,
    // Tolerate fetch failures during the gateway restart window — TanStack
    // retries on its own cadence; a missed poll just defers the edge
    // detection by one interval (the watchdog still backstops).
  });

  // Bypass poll — readable during a pause (unlike the /api/status query above,
  // which 503s). Sole source of the stranded-pause signal.
  const { data: clusterStatus } = useQuery({
    queryKey: CLUSTER_STATUS_QUERY_KEY,
    queryFn: api.getClusterStatus,
    refetchInterval: POLL_MS,
  });

  // Stranded = paused with no orchestration alive: a rollout was hard-killed and
  // left the pause + lock behind (a healthy rollout clears both together on
  // finish). Undefined data (initial / fetch fail) is not stranded — never offer
  // recovery without a confirmed snapshot.
  const stranded =
    clusterStatus != null &&
    clusterStatus.paused &&
    clusterStatus.current_orchestration == null;

  useEffect(() => {
    setClusterStranded(stranded);
  }, [stranded, setClusterStranded]);

  const paused = data?.cluster.current_paused ?? false;
  // current_orchestration flips true the moment a rollout/restart's detached
  // session spawns — minutes before the gateway pauses itself — and stays
  // true for the whole run. The banner keys off it (OR paused) so it shows for
  // the entire update, matching the Settings buttons, instead of only the late
  // paused window. The reconnect edge below still keys off `paused` alone: that
  // tracks the actual gateway bounce, which is what severs the SSE socket.
  const updating = paused || data?.cluster.current_orchestration != null;

  // Mirror the updating state into the store. Only act on successful data —
  // a failed poll (data === undefined) must not flip clusterUpdating to false,
  // or the "cluster updating" message disappears during the gateway restart
  // window (exactly when the auth guard needs it to show the updating page
  // instead of redirecting to login).
  useEffect(() => {
    if (data !== undefined) {
      setClusterUpdating(updating);
    }
  }, [data, updating, setClusterUpdating]);

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
