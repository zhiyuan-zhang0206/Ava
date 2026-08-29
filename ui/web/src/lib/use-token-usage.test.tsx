// useTokenUsage agent-switch tests — the context-bar snapshot must reflect the
// NEW agent's tokens immediately on switch-back (task #1939). The query carries
// staleTime 30s, so a hot switch-back (within 30s of the previous visit) serves
// the cached snapshot and — because a key change on an already-mounted observer
// only refetches when the cache is STALE — never refetches on its own; the hook
// must invalidate to force the background refresh. An idle agent emits no SSE
// token_usage event, so without that invalidate the bar would keep the previous
// visit's numbers until the next switch.

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
  it("forces a background refresh on hot switch-back", async () => {
    getTokenUsage.mockImplementation((id) => Promise.resolve(tokenFixture(id)));
    // Mirror the app: the global staleTime makes a just-visited cache NOT stale,
    // which is exactly the condition under which the key-change path skips the
    // refetch and the hook's invalidate must carry it.
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
    expect(getTokenUsage).toHaveBeenLastCalledWith(1);

    // Switch to another agent (cold): fetches on its own.
    rerender({ id: 2 });
    await waitFor(() => expect(getTokenUsage).toHaveBeenCalledTimes(2));
    expect(getTokenUsage).toHaveBeenLastCalledWith(2);

    // Switch back within staleTime (hot cache): the observer serves the cached
    // snapshot and would not refetch; the hook must invalidate to refresh.
    rerender({ id: 1 });
    await waitFor(() => expect(getTokenUsage).toHaveBeenCalledTimes(3));
    expect(getTokenUsage).toHaveBeenLastCalledWith(1);
  });
});
