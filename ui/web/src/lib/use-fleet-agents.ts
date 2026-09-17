"use client";
import { useAgentRoster } from "./use-agents";
import type { AgentRow } from "./types";
const EMPTY_AGENTS: AgentRow[] = [];
/** Fleet and sidebar share one coherent, bounded live roster. */
export function useFleetAgents(): AgentRow[] {
  return useAgentRoster().data?.agents ?? EMPTY_AGENTS;
}
