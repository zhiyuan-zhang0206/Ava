"use client";

// One coherent live tree; a selected historical conversation is a separate
// ID-addressed read. Neither selection nor lineage depends on loading history.
import { skipToken, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";
import { api } from "./api";
import { errMsg } from "./errors";
import { AGENTS_QUERY_KEY, AGENT_DETAIL_QUERY_KEY } from "./fold/agents";
import { useStore } from "./store";
import { useAgentActions } from "./use-agent-actions";
import type { AgentRoster } from "./types";

export { AGENTS_QUERY_KEY } from "./fold/agents";
export type { PendingAction } from "./use-agent-actions";
const EMPTY_ROSTER: AgentRoster = { agents: [], ancestors: [] };
const ACTIVE_ID_KEY = "ava.active.agent_id";

export function useAgentRoster() {
  return useQuery({
    queryKey: AGENTS_QUERY_KEY,
    queryFn: ({ signal }) => api.getAgentRoster(signal),
    staleTime: Infinity,
  });
}

export function useAgents(showError: (msg: string) => void) {
  const queryClient = useQueryClient();
  const roster = useAgentRoster();
  const { agents, ancestors } = roster.data ?? EMPTY_ROSTER;
  const activeId = useStore((state) => state.activeId);
  const setActiveId = useStore((state) => state.setActiveId);
  const hydrated = useRef(false);
  const liveSelected = agents.find((agent) => agent.agent_id === activeId);
  const selected = useQuery({
    queryKey: [...AGENT_DETAIL_QUERY_KEY, activeId],
    queryFn: activeId == null ? skipToken : ({ signal }) => api.getAgent(activeId, signal),
    enabled: activeId != null && liveSelected == null,
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
  });
  const activeAgent = liveSelected ?? selected.data;
  const actionAgents = activeAgent && !liveSelected ? [...agents, activeAgent] : agents;
  const actions = useAgentActions(showError, actionAgents);

  useEffect(() => {
    if (roster.error) showError(`Agent roster: ${errMsg(roster.error)}`);
    if (selected.error) showError(`Selected agent: ${errMsg(selected.error)}`);
  }, [roster.error, selected.error, showError]);

  useEffect(() => {
    const raw = new URL(window.location.href).searchParams.get("agent_id");
    let remembered: string | null = null;
    try { remembered = localStorage.getItem(ACTIVE_ID_KEY); } catch { /* storage can be disabled */ }
    const candidate = [raw, remembered].map(Number).find((id) => Number.isSafeInteger(id) && id > 0);
    if (candidate != null) setActiveId(candidate);
    hydrated.current = true;
  }, [setActiveId]);

  useEffect(() => {
    if (!hydrated.current) return;
    const id = useStore.getState().activeId;
    try {
      if (id == null) localStorage.removeItem(ACTIVE_ID_KEY);
      else localStorage.setItem(ACTIVE_ID_KEY, String(id));
    } catch { /* selection still works when storage is disabled */ }
    const url = new URL(window.location.href);
    if (id == null) url.searchParams.delete("agent_id");
    else url.searchParams.set("agent_id", String(id));
    if (url.toString() !== window.location.href) window.history.replaceState(null, "", url);
  }, [activeId]);

  useEffect(() => {
    if (!roster.isSuccess || useStore.getState().activeId != null) return;
    if (agents.length) setActiveId(agents[0].agent_id);
  }, [agents, roster.isSuccess, setActiveId]);

  const refresh = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: AGENTS_QUERY_KEY });
  }, [queryClient]);
  return { agents, ancestors, activeAgent, activeId, setActiveId, isLoading: roster.isLoading, refresh, ...actions };
}
export type UseAgentsResult = ReturnType<typeof useAgents>;
