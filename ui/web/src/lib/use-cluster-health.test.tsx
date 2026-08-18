// useClusterHealth tests — the update-done detector.
//
// Pins down the behaviors that make SSE update-aware:
//   1. The "updating" store flag (clusterUpdating, drives the banner) reflects
//      `paused` OR `current_orchestration` — so it shows for the whole rollout,
//      including the early window where the orchestration session is alive but
//      the gateway has not paused itself yet.
//   2. The paused true -> false edge (a rollout/restart just finished)
//      bumps reconnectNonce (reconnect SSE) AND refetches the agents query.
//      A first poll that is already unpaused (false -> false) must NOT fire,
//      and an orchestration-only change (paused never flips) must NOT fire —
//      the reconnect edge tracks the gateway bounce, which is `paused`.
//   3. The hook polls ONLY /api/cluster/status (the 503-bypassing snapshot) —
//      it must never touch the heavier /api/status, whose freshness belongs
//      to route-level observers.
//
// api.getClusterStatus is mocked; a real QueryClient is used so the
// refetchQueries call is observable via a spy.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { useStore } from "./store";
import { AGENTS_QUERY_KEY } from "./use-agents";
import { CLUSTER_STATUS_QUERY_KEY, useClusterHealth } from "./use-cluster-health";

vi.mock("./api", () => ({
  api: {
    getSystemStatus: vi.fn(),
    getClusterStatus: vi.fn(),
  },
}));

const getSystemStatus = vi.mocked(api.getSystemStatus);
const getClusterStatus = vi.mocked(api.getClusterStatus);

// Minimal ClusterStatus shaped enough for the hook (it reads only
// paused + current_orchestration). Cast through unknown — the rest of the
// ClusterStatus shape is irrelevant to this hook.
function clusterStatus(paused: boolean, orchestration: string | null = null) {
  return {
    paused,
    current_orchestration: orchestration,
  } as unknown as Awaited<ReturnType<typeof api.getClusterStatus>>;
}

function withClient(client: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client }, children);
  };
}

function freshClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
}

beforeEach(() => {
  getSystemStatus.mockReset();
  getClusterStatus.mockReset();
  // Reset the slices this hook writes to.
  act(() => {
    useStore.setState({ clusterUpdating: false, clusterStranded: false, reconnectNonce: 0 });
  });
});

afterEach(cleanup);

describe("useClusterHealth", () => {
  it("mirrors paused=true into store.clusterUpdating", async () => {
    getClusterStatus.mockResolvedValue(clusterStatus(true, "rollout"));
    const client = freshClient();

    renderHook(() => useClusterHealth(), { wrapper: withClient(client) });

    await waitFor(() => expect(useStore.getState().clusterUpdating).toBe(true));
  });

  it("current_orchestration set while not paused still drives clusterUpdating", async () => {
    // The early-rollout window: the orchestration session is alive but the
    // gateway has not paused itself yet. The banner must still show.
    getClusterStatus.mockResolvedValue(clusterStatus(false, "rollout"));
    const client = freshClient();

    renderHook(() => useClusterHealth(), { wrapper: withClient(client) });

    await waitFor(() => expect(useStore.getState().clusterUpdating).toBe(true));
  });

  it("orchestration clearing while paused stays false does NOT bump reconnect", async () => {
    const client = freshClient();
    const refetchSpy = vi.spyOn(client, "refetchQueries");

    // Orchestration in flight (banner on), then it ends — but paused never
    // flipped, so the gateway never bounced and SSE was never severed.
    getClusterStatus.mockResolvedValueOnce(clusterStatus(false, "rollout"));
    getClusterStatus.mockResolvedValue(clusterStatus(false, null));

    renderHook(() => useClusterHealth(), { wrapper: withClient(client) });

    await waitFor(() => expect(useStore.getState().clusterUpdating).toBe(true));

    await act(async () => {
      await client.refetchQueries({ queryKey: CLUSTER_STATUS_QUERY_KEY });
    });

    await waitFor(() => expect(useStore.getState().clusterUpdating).toBe(false));

    // No paused edge -> no reconnect, no agents refetch.
    expect(useStore.getState().reconnectNonce).toBe(0);
    expect(refetchSpy).not.toHaveBeenCalledWith({ queryKey: AGENTS_QUERY_KEY });
  });

  it("paused true -> false bumps reconnectNonce and refetches agents", async () => {
    const client = freshClient();
    const refetchSpy = vi.spyOn(client, "refetchQueries");

    // First resolve while paused, then flip to unpaused on the next poll.
    getClusterStatus.mockResolvedValueOnce(clusterStatus(true, "rollout"));
    getClusterStatus.mockResolvedValue(clusterStatus(false));

    renderHook(() => useClusterHealth(), { wrapper: withClient(client) });

    // Wait until we've observed the paused state (edge armed).
    await waitFor(() => expect(useStore.getState().clusterUpdating).toBe(true));

    const nonceWhilePaused = useStore.getState().reconnectNonce;

    // Force the next poll (the mock now returns unpaused).
    await act(async () => {
      await client.refetchQueries({ queryKey: CLUSTER_STATUS_QUERY_KEY });
    });

    await waitFor(() => expect(useStore.getState().clusterUpdating).toBe(false));

    // The edge fired: nonce bumped + agents refetched.
    expect(useStore.getState().reconnectNonce).toBe(nonceWhilePaused + 1);
    expect(refetchSpy).toHaveBeenCalledWith({ queryKey: AGENTS_QUERY_KEY });
  });

  it("a cold start that is already unpaused does NOT bump reconnect (false -> false)", async () => {
    getClusterStatus.mockResolvedValue(clusterStatus(false));
    const client = freshClient();
    const refetchSpy = vi.spyOn(client, "refetchQueries");

    renderHook(() => useClusterHealth(), { wrapper: withClient(client) });

    await waitFor(() => expect(getClusterStatus).toHaveBeenCalled());
    // Let any effects settle.
    await act(async () => {
      await Promise.resolve();
    });

    expect(useStore.getState().reconnectNonce).toBe(0);
    expect(useStore.getState().clusterUpdating).toBe(false);
    expect(refetchSpy).not.toHaveBeenCalledWith({ queryKey: AGENTS_QUERY_KEY });
  });

  it("paused + no orchestration sets clusterStranded", async () => {
    getClusterStatus.mockResolvedValue(clusterStatus(true, null));
    const client = freshClient();

    renderHook(() => useClusterHealth(), { wrapper: withClient(client) });

    await waitFor(() => expect(useStore.getState().clusterStranded).toBe(true));
  });

  it("paused WITH a live orchestration is NOT stranded (normal rollout)", async () => {
    getClusterStatus.mockResolvedValue(clusterStatus(true, "rollout"));
    const client = freshClient();

    renderHook(() => useClusterHealth(), { wrapper: withClient(client) });

    // Give the poll a chance to land + effects to settle.
    await waitFor(() => expect(getClusterStatus).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
    });
    expect(useStore.getState().clusterStranded).toBe(false);
  });

  it("never touches /api/status — that key's freshness belongs to route-level observers", async () => {
    getClusterStatus.mockResolvedValue(clusterStatus(false));
    const client = freshClient();

    renderHook(() => useClusterHealth(), { wrapper: withClient(client) });

    await waitFor(() => expect(getClusterStatus).toHaveBeenCalled());
    expect(getSystemStatus).not.toHaveBeenCalled();
  });
});
