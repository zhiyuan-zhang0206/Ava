// useFleetAgents — read-only live agent list for the /fleet view.
//
// A pure reader of the shared AGENTS_QUERY_KEY cache: one fetch, then the
// cache stays live because the SINGLE writer, the R4 fold owner (mounted at
// the app root), folds AgentSpawned / AgentUpdated / LabelUpdated events and
// issues the reconnect refetch. The fleet view no longer runs its own SSE
// merge — that duplicate writer (which also lacked the empty-cache seeding
// guard) is gone, so it can never seed a single-agent partial. No polling, no
// optimistic writes — the cache mirrors server truth, so the fleet never shows
// a state the backend later contradicts.

"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import type { AgentRow } from "./types";
import { AGENTS_QUERY_KEY, fetchAgentRoster } from "./use-agents";

// Module-level stable empty default — mirrors EMPTY_GRAPH / EMPTY_TASKS in the
// sibling fleet hooks. A `= []` inline default would construct a NEW array on
// every render while `data` is undefined (the pre-first-fetch window), giving
// the memoized DesktopLayout / MobileLayout an unstable `agents` prop that
// re-renders them on any parent render until the first fetch resolves.
const EMPTY_AGENTS: AgentRow[] = [];

export function useFleetAgents(): AgentRow[] {
  const queryClient = useQueryClient();
  const { data: agents = EMPTY_AGENTS } = useQuery({
    queryKey: AGENTS_QUERY_KEY,
    queryFn: () => fetchAgentRoster(queryClient, "live"),
    // SSE-driven (same cache as useAgents, kept fresh by the root fold at
    // the root) — no refetch on navigating to the fleet view.
    staleTime: Infinity,
  });

  return agents;
}
