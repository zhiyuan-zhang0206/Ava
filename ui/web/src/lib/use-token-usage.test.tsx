// Each selected context reads its latest token snapshot on activation.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { useTokenUsage } from "./use-token-usage";
import type { TokenUsageResponse } from "./types";

const { getTokenUsage } = vi.hoisted(() => ({
  getTokenUsage: vi.fn<(agentId: number) => Promise<TokenUsageResponse>>(),
}));
vi.mock("./api", () => ({ api: { getTokenUsage } }));
// The hook subscribes to the shared per-agent SSE stream; a no-op is enough —
// this test covers the REST snapshot refresh, not SSE folding (covered by the
// timeline-store tests).
vi.mock("./useEventStream", () => ({ useAgentEventStream: () => undefined }));

function tokenFixture(agentId: number): TokenUsageResponse {
  return {
    input_tokens: 1000 * agentId,
    output_tokens: 100,
    reasoning_tokens: 0,
    max_input_tokens: 200_000,
    soft_compact_tokens: 100_000,
    hard_compact_tokens: 150_000,
  };
}

afterEach(() => {
  cleanup();
  getTokenUsage.mockReset();
});

describe("useTokenUsage agent switch", () => {
  it("refreshes when returning to a previously selected agent", async () => {
    getTokenUsage.mockImplementation((id) => Promise.resolve(tokenFixture(id)));
    // A global cache default must not suppress activation reads.
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 5 * 60_000 } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const { rerender } = renderHook(
      ({ id }: { id: number | null }) => useTokenUsage(id, () => undefined),
      { initialProps: { id: 1 }, wrapper },
    );

    // Cold first visit: the observer fetches on its own.
    await waitFor(() => expect(getTokenUsage).toHaveBeenCalledTimes(1));
    expect(getTokenUsage).toHaveBeenLastCalledWith(1, expect.any(AbortSignal) as AbortSignal);

    // Switch to another agent (cold): fetches on its own.
    rerender({ id: 2 });
    await waitFor(() => expect(getTokenUsage).toHaveBeenCalledTimes(2));
    expect(getTokenUsage).toHaveBeenLastCalledWith(2, expect.any(AbortSignal) as AbortSignal);

    // Returning reads again even when the selection changed only moments ago.
    rerender({ id: 1 });
    await waitFor(() => expect(getTokenUsage).toHaveBeenCalledTimes(3));
    expect(getTokenUsage).toHaveBeenLastCalledWith(1, expect.any(AbortSignal) as AbortSignal);
  });

  it("reports contextPending until the cold key's first snapshot lands", async () => {
    let resolveUsage: (v: TokenUsageResponse) => void = () => undefined;
    getTokenUsage.mockImplementation(
      () =>
        new Promise<TokenUsageResponse>((resolve) => {
          resolveUsage = resolve;
        }),
    );
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const initialProps: { id: number | null } = { id: 7 };
    const { result, rerender } = renderHook(
      ({ id }: { id: number | null }) => useTokenUsage(id, () => undefined),
      { initialProps, wrapper },
    );

    // Cold key: pending until the first snapshot lands.
    expect(result.current.contextPending).toBe(true);

    await waitFor(() => expect(getTokenUsage).toHaveBeenCalled());
    resolveUsage(tokenFixture(7));
    await waitFor(() => expect(result.current.contextPending).toBe(false));
    expect(result.current.contextTokens).toBe(7000);

    // No active agent: nothing is loading, so never "pending".
    rerender({ id: null });
    expect(result.current.contextPending).toBe(false);
  });
});
