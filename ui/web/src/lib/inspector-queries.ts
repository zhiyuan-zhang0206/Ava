import { api } from "./api";

export const COMPACT_INSPECT_WINDOW = -1;

export const inspectLiveQueryKey = (agentId: number) =>
  ["agent-inspect-live", agentId] as const;

export const inspectWindowedQueryKey = (agentId: number, hours: number | null) =>
  ["agent-inspect", agentId, hours] as const;

export const inspectWidgetsQueryKey = (agentId: number) =>
  ["agent-inspect-widgets", agentId] as const;

export function fetchWindowedInspect(
  agentId: number,
  hours: number | null,
  signal?: AbortSignal,
) {
  return api.getAgentInspectStatistics(
    agentId,
    hours === COMPACT_INSPECT_WINDOW ? null : hours,
    hours === COMPACT_INSPECT_WINDOW,
    signal,
  );
}
