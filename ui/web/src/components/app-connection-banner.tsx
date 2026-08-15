"use client";

// AppConnectionBanner — the app-wide resilience provider, mounted once at the
// root (components/providers.tsx).
//
// It owns three concerns:
//   - useClusterHealth(): polls the cluster's paused flag and, on the
//     paused→false edge (a rollout finished), reconnects SSE + refetches agents.
//   - SSE connection-health tracking: subscribes to the GLOBAL /api/system
//     broadcast and mirrors connection state into the Zustand store so any
//     component (timeline, auth guard) can read it without its own subscription.
//   - Stranded-cluster recovery: renders the recovery banner at the app root
//     (critical — needs to be visible on every page, not just the timeline).
//
// SSE disconnect / reconnecting banners moved to the timeline notification
// area (ConnectionNotice) — they no longer render here. The separate
// "cluster updating" banner was dropped entirely: an unauthenticated reload
// during a rollout already gets the full-screen UpdatingPage (see
// auth-guard.tsx), and an already-open tab just sees the same SSE-drop notice
// as any other disconnect — a redundant "updating" banner on top added no
// information. Only the stranded recovery (needs operator action) stays at
// the root.
//
// Auth-gated: the whole thing only mounts (and only then run its hooks / poll)
// once authenticated. Pre-auth and on /login the login screen owns the viewport
// and the cluster pollers would just 401.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { Loader2 } from "lucide-react";

import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { errMsg } from "@/lib/errors";
import { useStore } from "@/lib/store";
import { AGENTS_QUERY_KEY } from "@/lib/use-agents";
import {
  CLUSTER_STATUS_QUERY_KEY,
  SYSTEM_STATUS_QUERY_KEY,
  useClusterHealth,
} from "@/lib/use-cluster-health";
import type { ConnectionEvent } from "@/lib/useEventStream";
import { useEventStream } from "@/lib/useEventStream";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

export function AppConnectionBanner() {
  const { status } = useAuth();
  // Conditional MOUNT (not conditional hooks): the pollers + SSE subscription only
  // come alive once authenticated, so nothing 401s behind the login screen.
  if (status !== "authenticated") return null;
  return <ConnectionHealthProvider />;
}

function ConnectionHealthProvider() {
  const queryClient = useQueryClient();
  const setConnState = useStore((s) => s.setConnState);
  const clusterStranded = useStore((s) => s.clusterStranded);
  const bumpReconnect = useStore((s) => s.bumpReconnect);
  const showToast = useStore((s) => s.showToast);

  // Poll cluster status + drive the reconnect-on-update-done edge. Single mount.
  useClusterHealth();

  // Track the GLOBAL broadcast's connection health and mirror it into the store
  // so the timeline's ConnectionNotice can read it.
  const onSystemEvent = useCallback(() => {
    // Business events are folded by the root fold (EventStreamProvider) — nothing to do
    // here; this subscriber exists only to observe connection health.
  }, []);
  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      if (ev.type === "parse-failed") return;
      setConnState(ev.type);
    },
    [setConnState],
  );
  useEventStream(onSystemEvent, onConnectionEvent);

  // Stranded-cluster recovery: force-clear the leftover pause + update lock, then
  // refetch cluster state + agents and reopen SSE so the unblocked UI catches up.
  // This is the ONLY visual banner that still renders at the root — it needs
  // operator action and must be visible on every page.
  const recover = useMutation({
    mutationFn: api.recoverCluster,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: CLUSTER_STATUS_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: SYSTEM_STATUS_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: AGENTS_QUERY_KEY });
      bumpReconnect();
    },
    onError: (err: unknown) => showToast(`Recover failed: ${errMsg(err)}`),
  });

  if (clusterStranded) {
    return (
      <div
        role="status"
        aria-live="polite"
        className={cn("flex-wrap items-center gap-x-2 gap-y-1 bg-destructive/15 text-destructive border-b border-destructive/40 px-4 py-1 text-xs", FLEX)}
      >
        <span>
          Cluster paused but no update is running — a rollout was likely interrupted.
        </span>
        <button
          type="button"
          onClick={() => recover.mutate()}
          disabled={recover.isPending}
          className="inline-flex items-center gap-1 rounded border border-destructive/50 px-1.5 py-0.5 font-medium hover:bg-destructive/10 disabled:opacity-60"
        >
          {recover.isPending ? <Loader2 className="size-3 animate-spin" aria-hidden /> : null}
          {recover.isPending ? "Recovering…" : "Resume cluster"}
        </button>
      </div>
    );
  }

  return null;
}
