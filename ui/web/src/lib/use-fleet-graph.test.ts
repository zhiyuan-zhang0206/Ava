// useFleetGraph tests — the API fetch with no client-side fallback.
//
// The hook has three observable outcomes:
//   1. while the /api/fleet/graph query is pending -> an empty graph,
//      loading=true, error=false;
//   2. once the query succeeds -> the API graph (normalized), loading=false,
//      error=false;
//   3. if the query errors (404 / gateway down) -> an empty graph, loading=false,
//      error=true (no retry).
//
// api.getFleetGraph is mocked; a real QueryClient (retry off) drives the query so
// the success/error edges are exercised end to end.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import type { WireFleetGraph } from "./types";
import { useFleetGraph } from "./use-fleet-graph";

vi.mock("./api", () => ({
  api: { getFleetGraph: vi.fn() },
}));

// useEventStream is a context-driven hook that needs a Provider ancestor.
// Mock it out — the event-driven refetch is tested by the useEventStream
// suite; here we only care about the query + normalization path.
vi.mock("./useEventStream", () => ({
  useEventStream: vi.fn(),
  EventStreamProvider: ({ children }: { children: React.ReactNode }) =>
    React.createElement(React.Fragment, null, children),
}));

const getFleetGraph = vi.mocked(api.getFleetGraph);

const API_GRAPH: WireFleetGraph = {
  nodes: [
    {
      agent_id: 7,
      label: "from-api",
      status: "running",
      liveness_state: "online",
      spawner: "user",
      machine: "test",
      node_score: 0,
      total_tokens: 0,
    },
  ],
  edges: [
    {
      from_agent: 7,
      to_agent: 7,
      event_type: "message",
      weight: 0.5,
      event_count: 2,
      last_seen_at: "2026-06-17T00:00:00Z",
    },
  ],
  stale: false,
};

function withClient(client: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client }, children);
  };
}

function freshClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

beforeEach(() => {
  getFleetGraph.mockReset();
});

afterEach(cleanup);

describe("useFleetGraph", () => {
  it("while pending: empty graph, loading=true, error=false", () => {
    // Never resolves -> the query stays pending for the duration of the assertion.
    getFleetGraph.mockReturnValue(new Promise<WireFleetGraph>(() => {
      /* never settles — keeps the query pending */
    }));

    const { result } = renderHook(() => useFleetGraph(), {
      wrapper: withClient(freshClient()),
    });

    expect(result.current.graph).toEqual({ nodes: [], edges: [], stale: false });
    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBe(false);
  });

  it("on success: returns the API graph, loading=false, error=false", async () => {
    getFleetGraph.mockResolvedValue(API_GRAPH);

    const { result } = renderHook(() => useFleetGraph(), {
      wrapper: withClient(freshClient()),
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.graph).toEqual(API_GRAPH);
    expect(result.current.error).toBe(false);
  });

  it("normalizes the raw backend shape: from_agent/to_agent + send_message->message", async () => {
    // The real endpoint returns Pydantic field names (from_agent/to_agent) and the
    // raw event_log value `send_message`; normalizeGraph maps them. Lineage types
    // (spawn/fork/resurrect) pass through unchanged.
    const raw = {
      nodes: [],
      edges: [
        { from_agent: 1, to_agent: 2, event_type: "send_message", weight: 0.5, event_count: 3, last_seen_at: "2026-06-30T00:00:00Z" },
        { from_agent: 2, to_agent: 3, event_type: "resurrect", weight: 4, event_count: 2 },
      ],
    } as unknown as WireFleetGraph;
    getFleetGraph.mockResolvedValue(raw);

    const { result } = renderHook(() => useFleetGraph(), {
      wrapper: withClient(freshClient()),
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.graph.edges).toEqual([
      { from_agent: 1, to_agent: 2, event_type: "message", weight: 0.5, event_count: 3, last_seen_at: "2026-06-30T00:00:00Z" },
      { from_agent: 2, to_agent: 3, event_type: "resurrect", weight: 4, event_count: 2 },
    ]);
  });

  it("projects raw fleet-node lifecycle states to the same public three-state model", async () => {
    const raw = {
      nodes: [
        { ...API_GRAPH.nodes[0], agent_id: 1, status: "running" },
        { ...API_GRAPH.nodes[0], agent_id: 2, status: "idling" },
        { ...API_GRAPH.nodes[0], agent_id: 3, status: "restarting" },
        { ...API_GRAPH.nodes[0], agent_id: 4, status: "terminated" },
        { ...API_GRAPH.nodes[0], agent_id: 5, status: "hibernating" },
      ],
      edges: [],
    } as unknown as WireFleetGraph;
    getFleetGraph.mockResolvedValue(raw);

    const { result } = renderHook(() => useFleetGraph(), {
      wrapper: withClient(freshClient()),
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.graph.nodes.map((node) => node.status)).toEqual([
      "running",
      "idling",
      "idling",
      "terminated",
      "idling",
    ]);
  });

  it("preserves offline liveness while projecting a restarting node to idling", async () => {
    const raw = {
      nodes: [
        {
          ...API_GRAPH.nodes[0],
          status: "restarting",
          liveness_state: "offline",
        },
      ],
      edges: [],
      stale: false,
    } as unknown as WireFleetGraph;
    getFleetGraph.mockResolvedValue(raw);

    const { result } = renderHook(() => useFleetGraph(), {
      wrapper: withClient(freshClient()),
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.graph.nodes[0]).toMatchObject({
      status: "idling",
      liveness_state: "offline",
    });
  });

  it("on error: empty graph, loading=false, error=true (no retry)", async () => {
    getFleetGraph.mockRejectedValue(new Error("HTTP 404: no such endpoint"));

    const { result } = renderHook(() => useFleetGraph(), {
      wrapper: withClient(freshClient()),
    });

    await waitFor(() => expect(result.current.error).toBe(true));
    expect(result.current.loading).toBe(false);
    expect(result.current.graph).toEqual({ nodes: [], edges: [], stale: false });
  });

  it("preserves the generated stale flag and renders a degraded 200 as unavailable", async () => {
    getFleetGraph.mockResolvedValue({ nodes: [], edges: [], stale: true });

    const { result } = renderHook(() => useFleetGraph(), {
      wrapper: withClient(freshClient()),
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.graph.stale).toBe(true);
    expect(result.current.error).toBe(true);
  });

  it("stale-while-error: a failed poll after data keeps the last good graph (error flagged, not blanked)", async () => {
    // First poll succeeds, every later poll fails — the graph must stay put.
    getFleetGraph.mockResolvedValueOnce(API_GRAPH);
    getFleetGraph.mockRejectedValue(new Error("gateway down"));
    const client = freshClient();

    const { result } = renderHook(() => useFleetGraph(), { wrapper: withClient(client) });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.graph).toEqual(API_GRAPH);
    expect(result.current.error).toBe(false);

    // Force the next poll (the mock now rejects).
    await act(async () => {
      await client.refetchQueries({ queryKey: ["fleet-graph"] });
    });

    await waitFor(() => expect(result.current.error).toBe(true));
    // The board is NOT cleared — the last-loaded graph is still served.
    expect(result.current.graph).toEqual(API_GRAPH);
  });
});
