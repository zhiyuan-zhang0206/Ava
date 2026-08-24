// Gate-only maintenance ownership + cluster reconnect edge.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { useStore } from "./store";
import { AGENTS_QUERY_KEY } from "./use-agents";
import {
  CLUSTER_STATUS_QUERY_KEY,
  useClusterHealth,
} from "./use-cluster-health";

vi.mock("./api", () => ({
  api: {
    getSystemStatus: vi.fn(),
    getClusterStatus: vi.fn(),
  },
}));

const getSystemStatus = vi.mocked(api.getSystemStatus);
const getClusterStatus = vi.mocked(api.getClusterStatus);

function clusterStatus(paused: boolean, orchestration: string | null = null) {
  return { paused, current_orchestration: orchestration } as unknown as Awaited<
    ReturnType<typeof api.getClusterStatus>
  >;
}

function withClient(client: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client }, children);
  };
}

function freshClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

beforeEach(() => {
  getSystemStatus.mockReset();
  getClusterStatus.mockReset();
  getClusterStatus.mockResolvedValue(clusterStatus(false));
  act(() => useStore.setState({ clusterStranded: false, reconnectNonce: 0 }));
});

afterEach(cleanup);

describe("useClusterHealth", () => {
  it("paused true to false reconnects SSE and refetches agents", async () => {
    const client = freshClient();
    const refetchSpy = vi.spyOn(client, "refetchQueries");
    getClusterStatus.mockResolvedValueOnce(clusterStatus(true, "rollout"));
    getClusterStatus.mockResolvedValue(clusterStatus(false));
    renderHook(() => useClusterHealth(), { wrapper: withClient(client) });

    await waitFor(() =>
      expect(client.getQueryData(CLUSTER_STATUS_QUERY_KEY)).toMatchObject({ paused: true }),
    );
    const nonce = useStore.getState().reconnectNonce;
    await act(async () => {
      await client.refetchQueries({ queryKey: CLUSTER_STATUS_QUERY_KEY });
    });

    await waitFor(() => expect(useStore.getState().reconnectNonce).toBe(nonce + 1));
    expect(refetchSpy).toHaveBeenCalledWith({ queryKey: AGENTS_QUERY_KEY });
  });

  it("paused with no orchestration sets the stranded recovery state", async () => {
    getClusterStatus.mockResolvedValue(clusterStatus(true, null));
    renderHook(() => useClusterHealth(), { wrapper: withClient(freshClient()) });

    await waitFor(() => expect(useStore.getState().clusterStranded).toBe(true));
  });

  it("never touches the heavier system status endpoint", async () => {
    renderHook(() => useClusterHealth(), { wrapper: withClient(freshClient()) });

    await waitFor(() => expect(getClusterStatus).toHaveBeenCalled());
    expect(getSystemStatus).not.toHaveBeenCalled();
  });
});
