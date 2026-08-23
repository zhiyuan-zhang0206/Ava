// AppConnectionBanner tests — the root-mounted resilience provider.
//
// Verifies:
//   1. Auth-gated: nothing mounts (no cluster poll, no SSE subscription) until
//      authenticated — pre-auth the login screen owns the viewport.
//   2. The stranded-cluster recovery affordance renders from the store flag.
//   3. SSE connection-health tracking writes to the store (connState) so the
//      timeline's ConnectionNotice can read it. No visual banner for
//      non-stranded states — those moved to ConnectionNotice in the timeline.
//
// useClusterHealth + useEventStream are mocked (their behavior is covered in
// their own suites); the real Zustand store is used so the wiring is exercised.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useStore } from "@/lib/store";
import { CLUSTER_STATUS_QUERY_KEY } from "@/lib/use-cluster-health";
import type { ConnectionEvent } from "@/lib/useEventStream";

import { AppConnectionBanner } from "./app-connection-banner";

// Auth state, mutable per test.
const authState: { status: string } = { status: "authenticated" };
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => authState,
}));

// Cluster-health poller — a spy so we can assert it only mounts once authed.
const { useClusterHealth } = vi.hoisted(() => ({ useClusterHealth: vi.fn() }));
vi.mock("@/lib/use-cluster-health", () => ({
  useClusterHealth,
  SYSTEM_STATUS_QUERY_KEY: ["system-status"],
  CLUSTER_STATUS_QUERY_KEY: ["cluster-status"],
}));

// Capture the connection handler so a test can drive SSE state transitions.
const { connRef, systemRef } = vi.hoisted(() => ({
  connRef: { current: null as ((ev: ConnectionEvent) => void) | null },
  systemRef: { current: null as ((ev: unknown) => void) | null },
}));
vi.mock("@/lib/useEventStream", () => ({
  useEventStream: (onSystem: (ev: unknown) => void, onConn: (ev: ConnectionEvent) => void) => {
    systemRef.current = onSystem;
    connRef.current = onConn;
  },
}));

vi.mock("@/lib/api", () => ({
  api: { recoverCluster: vi.fn(() => Promise.resolve({ unlocked_holder: null })) },
}));

function renderBanner() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return {
    queryClient: qc,
    ...render(
      <QueryClientProvider client={qc}>
        <AppConnectionBanner />
      </QueryClientProvider>,
    ),
  };
}

beforeEach(() => {
  authState.status = "authenticated";
  connRef.current = null;
  systemRef.current = null;
  useClusterHealth.mockClear();
  act(() => {
    useStore.setState({
      clusterUpdating: false,
      clusterStranded: false,
      connState: "open",
    });
  });
});

afterEach(cleanup);

describe("AppConnectionBanner", () => {
  it("renders nothing and mounts no pollers when unauthenticated", () => {
    authState.status = "unauthenticated";
    const { container } = renderBanner();
    expect(container.firstChild).toBeNull();
    expect(useClusterHealth).not.toHaveBeenCalled();
  });

  it("authenticated + healthy: mounts the cluster-health poller, shows no banner", () => {
    const { container } = renderBanner();
    expect(useClusterHealth).toHaveBeenCalled();
    // Healthy state — no stranded recovery needed, no banner at root.
    expect(container.firstChild).toBeNull();
  });

  it("shows the stranded-cluster recovery prompt from the store flag", () => {
    act(() => {
      useStore.setState({ clusterStranded: true });
    });
    renderBanner();
    expect(screen.getByRole("button", { name: /Resume cluster/i })).toBeTruthy();
    expect(screen.getByText(/no update is running/i)).toBeTruthy();
  });

  it("tracks the global SSE connection health and writes to the store", () => {
    renderBanner();
    expect(connRef.current).not.toBeNull();

    // Drive a 'closed' event — it should update the store's connState
    act(() => {
      connRef.current?.({ type: "closed" });
    });
    expect(useStore.getState().connState).toBe("closed");

    // Drive a 'reconnecting' event
    act(() => {
      connRef.current?.({ type: "reconnecting" });
    });
    expect(useStore.getState().connState).toBe("reconnecting");

    // Drive an 'open' event — back to healthy
    act(() => {
      connRef.current?.({ type: "open" });
    });
    expect(useStore.getState().connState).toBe("open");

    // parse-failed is ignored (not a health-state change)
    act(() => {
      connRef.current?.({ type: "parse-failed", raw: "x", error: new Error("x") });
    });
    expect(useStore.getState().connState).toBe("open");
  });

  it("marks the cluster as updating as soon as the global start event arrives", () => {
    const { queryClient } = renderBanner();
    expect(systemRef.current).not.toBeNull();

    act(() => {
      systemRef.current?.({
        role: "cluster_update_started",
        agent_id: 0,
        kind: "rollout",
        origin: "user",
      });
    });

    expect(useStore.getState().clusterUpdating).toBe(true);
    expect(queryClient.getQueryData(CLUSTER_STATUS_QUERY_KEY)).toMatchObject({
      paused: false,
      current_orchestration: "rollout",
    });
  });

  it("replaces a cached pre-update snapshot when the start event arrives", () => {
    const { queryClient } = renderBanner();
    queryClient.setQueryData(CLUSTER_STATUS_QUERY_KEY, {
      machine_name: "gateway-a",
      serve_gateway: true,
      serve_agent_runner: true,
      paused: true,
      current_orchestration: null,
      shell_count: 0,
    });

    act(() => {
      systemRef.current?.({
        role: "cluster_update_started",
        agent_id: 0,
        kind: "restart",
        origin: "agent:7",
      });
    });

    expect(queryClient.getQueryData(CLUSTER_STATUS_QUERY_KEY)).toMatchObject({
      machine_name: "gateway-a",
      paused: true,
      current_orchestration: "restart",
    });
  });
});
