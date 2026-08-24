// useFleetGraph — the data source for the fleet Graph View. It fetches the
// weighted /api/fleet/graph (spawn/fork lineage + aggregated message edges +
// dynamic weights) and polls it for fresh weights. There is no client-side
// fallback: the view reads the backend graph or shows an empty/error state.

"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { api } from "./api";
import { projectAgentStatusValue } from "./types";
import type { FleetGraph, GraphEventType, WireFleetGraph } from "./types";

const EMPTY_GRAPH: FleetGraph = {
  nodes: [],
  edges: [],
  stale: false,
  truncated: false,
  telemetry_stale: false,
  snapshot_at: null,
};

export interface FleetGraphResult {
  readonly graph: FleetGraph;
  readonly loading: boolean;
  readonly error: boolean;
}

/**
 * The backend emits the raw event_log value `send_message` for message ties —
 * map it to the graph's `message` vocabulary so the view can key on `message`
 * (lineage types pass through). This is the ONLY wire normalization left: the
 * edge fields already travel under the generated contract names (from_agent /
 * to_agent — see FleetGraphEdge in types.ts), so the old from/to-renaming
 * shim was a no-op copy.
 */
function normalizeEventType(raw: string): GraphEventType {
  return raw === "send_message" ? "message" : (raw as GraphEventType);
}

function normalizeGraph(raw: WireFleetGraph): FleetGraph {
  return {
    ...raw,
    nodes: raw.nodes.map((node) => ({
      ...node,
      status: projectAgentStatusValue(node.status),
    })),
    edges: raw.edges.map((rawEdge) => ({
      ...rawEdge,
      event_type: normalizeEventType(rawEdge.event_type),
    })),
  };
}

export function useFleetGraph(opts?: { hours?: number; decayLambda?: number }): FleetGraphResult {
  const hours = opts?.hours;
  const decayLambda = opts?.decayLambda;
  const { data, isError, isLoading } = useQuery({
    // hours / decayLambda are in the key so React Query refetches when the
    // window selector changes.
    queryKey: ["fleet-graph", hours ?? null, decayLambda ?? null],
    queryFn: () => api.getFleetGraph({ hours, decayLambda }),
    retry: false,
    // Slow reconciliation poll beneath the SSE invalidation (same pattern as
    // use-tasks.ts). The backend keeps a whole response for 60s
    // (_CACHE_TTL_SECONDS in gateway/routers/fleet_graph.py), so alternating
    // 30s polls hit Redis and expensive source reads run at most once a minute.
    // A constant interval keeps retrying failed polls instead of freezing.
    refetchInterval: 30_000,
  });

  // Liveness: the R4 fold owner (lib/fold/graph) invalidates this query on
  // spawn/update events (debounced); the poll below self-heals failures.

  const result = useMemo(() => {
    // Stale-while-error: keep serving the last good graph even if the latest poll
    // failed (error carries the failure). Only fall back to the empty graph when
    // nothing has loaded yet.
    if (data) return { graph: normalizeGraph(data), loading: false, error: isError || data.stale };
    return { graph: EMPTY_GRAPH, loading: isLoading, error: isError };
  }, [data, isError, isLoading]);
  return result;
}
