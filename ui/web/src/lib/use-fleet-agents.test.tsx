import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentRow } from "./types";

const { fetchAgentRoster } = vi.hoisted(() => ({ fetchAgentRoster: vi.fn() }));
vi.mock("./use-agents", () => ({
  AGENTS_QUERY_KEY: ["agents", "live"],
  fetchAgentRoster,
}));

import { useFleetAgents } from "./use-fleet-agents";

const AGENTS = [{ agent_id: 7, label: "fleet worker" }] as AgentRow[];

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(
    QueryClientProvider,
    { client: new QueryClient({ defaultOptions: { queries: { retry: false } } }) },
    children,
  );
}

afterEach(() => vi.clearAllMocks());

describe("useFleetAgents", () => {
  it("returns one stable empty list while the shared roster is loading", () => {
    fetchAgentRoster.mockReturnValue(new Promise<AgentRow[]>(() => undefined));
    const { result, rerender } = renderHook(() => useFleetAgents(), { wrapper });

    const empty = result.current;
    rerender();
    expect(result.current).toBe(empty);
    expect(result.current).toEqual([]);
  });

  it("reads the live shared roster through its cache fetcher", async () => {
    fetchAgentRoster.mockResolvedValue(AGENTS);
    const { result } = renderHook(() => useFleetAgents(), { wrapper });

    await waitFor(() => expect(result.current).toBe(AGENTS));
    expect(fetchAgentRoster).toHaveBeenCalledWith(expect.any(QueryClient), "live");
  });
});
