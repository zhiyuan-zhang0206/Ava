import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useStore } from "@/lib/store";
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

  it("authenticated: mounts the cluster-health poller and renders nothing", () => {
    const { container } = renderBanner();
    expect(useClusterHealth).toHaveBeenCalled();
    expect(container.firstChild).toBeNull();
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

});
