// useAgents — agent list + lifecycle actions, all driven by server truth.
//
// Reads through TanStack Query (`AGENTS_QUERY_KEY`); initial load is one
// fetch. The shared ["agents"] cache is kept live by the R4 fold (layer 1):
// EventStreamProvider's fold owner applies the AgentSpawned / AgentUpdated /
// LabelUpdated events into this cache (lib/fold/agents.ts). useAgents itself
// does not subscribe — it reads that cache and drives the lifecycle
// mutations. No polling, no optimistic writes — the cache mirrors the server
// byte for byte, so the sidebar never flips an item to a state the backend
// later contradicts.
//
// Lifecycle mutations (spawn / fork / terminate / restart / resurrect)
// flip `isPending` only; the sidebar shows a spinner on the affected row
// until the SSE event carrying the real new state replaces it. SpawningRow
// stays visible until the AgentSpawned event delivers the new agent_id —
// at which point the same render frame opens the real row.

"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";

import { api } from "./api";
import { errMsg } from "./errors";
import { useStore } from "./store";
import { useAgentActions } from "./use-agent-actions";
import { AGENTS_QUERY_KEY } from "./fold/agents";
import type { AgentRow } from "./types";

// R4 layer 1: the agents fold (upsert / label patch) lives in lib/fold/agents
// and runs inside EventStreamProvider; this module re-exports the merge rule
// for readers that share it (the fleet view) and the query key.
export { AGENTS_QUERY_KEY, upsertAgent } from "./fold/agents";

// EXEMPT from the localStorage→DB migration (kept per-device by design): the
// last-viewed agent is an ephemeral "which conversation am I looking at right
// now" selection, not a durable preference — each device / tab tracks its own,
// so it stays in localStorage rather than syncing through user_settings.
const ACTIVE_ID_KEY = "ava.active.agent_id";

/** TanStack Query cache key; shares one agents dataset across hooks. */


// Persist the last-viewed agent_id so the same conversation comes
// back after refresh. If the agent no longer exists (DB reset / id
// drift), the auto-select-after-refresh logic falls back to the first
// alive agent; no extra check needed here.
//
// SSR-friendly: useState can not use lazy init reading localStorage —
// the server side can not access it → server renders activeId=null,
// client hydrates with the real value, triggering hydration mismatch
// and leaving DOM attributes like disabled stuck (e.g. input appears
// disabled until the next genuine state change). Read in a post-mount
// useEffect instead so server/client initial values agree.
function readPersistedActiveId(): number | null {
  try {
    const raw = localStorage.getItem(ACTIVE_ID_KEY);
    if (!raw) return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  } catch {
    return null;
  }
}

import type { PendingAction } from "./use-agent-actions";
export type { PendingAction } from "./use-agent-actions";

export interface UseAgentsResult {
  agents: AgentRow[];
  activeId: number | null;
  setActiveId: (id: number | null) => void;
  pendingActions: Record<number, PendingAction>;
  pendingSpawnCount: number;
  forkPending: boolean;
  isLoading: boolean;
  spawn: (machine?: string, model?: string, preset?: string, reasoning_effort?: string) => Promise<number | null>;
  fork: (sourceId: number, prompt?: string) => Promise<number | null>;
  terminate: (id: number, force?: boolean) => Promise<void>;
  restart: (id: number) => Promise<void>;
  resurrect: (id: number, prompt?: string) => Promise<void>;
  compact: (id: number) => Promise<void>;
  refresh: () => Promise<void>;
}

export function useAgents(showError: (msg: string) => void): UseAgentsResult {
  const queryClient = useQueryClient();
  const { data: agents = [], error: agentsError, isLoading } = useQuery({
    queryKey: AGENTS_QUERY_KEY,
    queryFn: api.listAgents,
    // No refetchInterval, staleTime Infinity — the agent list is SSE-driven:
    // AgentSpawned / AgentUpdated events merge into the cache (here +
    // the root fold), so it is kept fresh without polling and
    // without a refetch on every navigation. On SSE reconnect we refetch once
    // to resync. The initial cold fetch still runs (no cached data yet).
    staleTime: Infinity,
  });

  // Lifecycle actions live in use-agent-actions.ts (R4 layer-1 line budget);
  // this hook keeps the reader half + the activeId handling.
  const {
    pendingActions,
    pendingSpawnCount,
    forkPending,
    spawn,
    fork,
    terminate,
    restart,
    resurrect,
    compact,
  } = useAgentActions(showError, agents);

  // useQuery swallows fetch errors into query state and does not throw on render — showError surfaces them as a toast
  useEffect(() => {
    if (agentsError) showError(`Failed to list agents: ${errMsg(agentsError)}`);
  }, [agentsError, showError]);

  // -- activeId uses the Zustand store as the single source --
  //    Components (AgentSidebar / HomeContent) read from the store; no
  //    prop-drilling needed. Writes (sidebar selection / spawn / fork /
  //    auto-select) all go through store.setActiveId.
  const activeId = useStore((s) => s.activeId);
  const setActiveId = useStore((s) => s.setActiveId);

  // Only write to localStorage after hydrated=true; otherwise the
  // first-mount effect would overwrite the persisted value with the
  // not-yet-restored null.
  const hydratedRef = useRef(false);

  // Restore activeId after mount: URL ?agent_id=N wins (deep-link / e2e);
  // otherwise localStorage. Invalid values (non-numeric / not in agents)
  // fall through to the auto-select fallback (the effect at line 116
  // resets). Runs after SSR render, so no hydration mismatch.
  useEffect(() => {
    const raw = new URL(window.location.href).searchParams.get("agent_id");
    const fromUrl = raw != null ? Number.parseInt(raw, 10) : Number.NaN;
    if (Number.isFinite(fromUrl) && fromUrl > 0) {
      setActiveId(fromUrl);
    } else {
      const persisted = readPersistedActiveId();
      if (persisted != null) setActiveId(persisted);
    }
    hydratedRef.current = true;
  }, [setActiveId]);

  useEffect(() => {
    if (!hydratedRef.current) return;
    // Read the latest activeId from the store (not the closure), so when
    // the URL-reading effect above calls setActiveId in the same microtask,
    // this effect sees the new value — not the stale pre-navigation one.
    // Without this, navigating from /fleet → /?agent_id=123 briefly wrote
    // the old activeId back to the URL before the re-render corrected it,
    // causing a visible flash of the wrong agent.
    const latest = useStore.getState().activeId;
    if (latest == null) localStorage.removeItem(ACTIVE_ID_KEY);
    else localStorage.setItem(ACTIVE_ID_KEY, String(latest));

    // Sync URL ?agent_id=N: after the user switches sidebar / spawns
    // a new agent, copying the URL gives a thread deep-link (share /
    // bookmark / open on phone). replaceState avoids polluting the
    // history stack — otherwise every thread switch would add one entry.
    const url = new URL(window.location.href);
    if (latest == null) url.searchParams.delete("agent_id");
    else url.searchParams.set("agent_id", String(latest));
    if (url.toString() !== window.location.href) {
      window.history.replaceState(null, "", url.toString());
    }
  }, [activeId]);

  // Auto-select activeId — fires for exactly two reasons, never merely
  // because the `agents` array changed reference (a heartbeat / activity /
  // label churn must not move the user's selection):
  //   1. initial load with nothing selected yet (prev == null)
  //   2. the selected agent has disappeared from the list — confirmed gone,
  //      e.g. terminated + pruned. A terminated-but-still-present agent keeps
  //      the selection so the user can read its history.
  // Both collapse to the same predicate: (re)select only when the current
  // selection is absent from a non-empty list. When it is still present we
  // return early, so a fresh array reference with the selection intact is a
  // no-op and never yanks the view (R17).
  //
  // The setQueryData guards above ensure agent_spawned / agent_updated /
  // label_updated never seed a partial cache before the initial listAgents
  // fetch lands, so the selected agent is never transiently absent from
  // the list during a cache merge.
  //
  // Guard agents.length === 0: on initial mount the query has not
  // resolved yet (agents defaults to [] from useQuery). Running
  // auto-select against the empty list would reset a just-restored
  // activeId (from URL / localStorage) back to null — then when the
  // query lands it picks the first alive agent, jumping away from the
  // user's previous selection.
  useEffect(() => {
    if (agents.length === 0) return;
    const prev = useStore.getState().activeId;
    if (prev != null && agents.some((a) => a.agent_id === prev)) return;
    const next = (agents.find((a) => a.status !== "terminated") ?? agents[0]).agent_id;
    // Idempotent: skip the write when the target already equals the current
    // selection, so we never emit a redundant setActiveId (which would ripple
    // into the URL / localStorage sync effects for no reason).
    if (next !== prev) setActiveId(next);
  }, [agents, setActiveId]);

  const refresh = useCallback(async () => {
    await queryClient.refetchQueries({ queryKey: AGENTS_QUERY_KEY });
  }, [queryClient]);

  // No SSE subscription here. The shared ["agents"] cache has a SINGLE
  // writer — the root fold (EventStreamProvider), which folds
  // agent_spawned / agent_updated / label_updated events and issues the
  // reconnect refetch. useAgents is a pure reader of that cache (plus the
  // lifecycle mutations below). Collapsing the writers here removes the
  // duplicate merge + duplicate reconnect refetch the home view used to run.

  return {
    agents,
    activeId,
    setActiveId,
    pendingActions,
    pendingSpawnCount,
    forkPending,
    isLoading,
    spawn,
    fork,
    terminate,
    restart,
    resurrect,
    compact,
    refresh,
  };
}
