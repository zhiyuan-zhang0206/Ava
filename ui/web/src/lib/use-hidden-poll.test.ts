import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { usePendingMessages } from "./use-pending-messages";
import { useTokenUsage } from "./use-token-usage";
import type { ConnectionEvent } from "./useEventStream";

vi.mock("./api", () => ({
  api: {
    getPendingMessages: vi.fn(),
    getTokenUsage: vi.fn(),
  },
}));

let connectionHandler: ((event: ConnectionEvent) => void) | null = null;

vi.mock("./useEventStream", () => ({
  useAgentEventStream: (
    _onEvent: unknown,
    onConnectionEvent: (event: ConnectionEvent) => void,
  ) => {
    connectionHandler = onConnectionEvent;
  },
}));

function pushPoll(): void {
  if (connectionHandler === null) throw new Error("hook did not subscribe to connection events");
  act(() => connectionHandler!({ type: "poll" }));
}

let queryClient: QueryClient;

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

beforeEach(() => {
  vi.clearAllMocks();
  connectionHandler = null;
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  vi.mocked(api.getPendingMessages).mockResolvedValue([]);
  vi.mocked(api.getTokenUsage).mockResolvedValue({
    input_tokens: 0,
    output_tokens: 0,
    reasoning_tokens: 0,
    max_input_tokens: 100_000,
    soft_compact_tokens: 70_000,
    hard_compact_tokens: 90_000,
  });
});

afterEach(() => cleanup());

describe("hidden-tab poll invalidation", () => {
  it("invalidates token usage for the active agent", async () => {
    renderHook(() => useTokenUsage(42, vi.fn()), { wrapper });
    await waitFor(() => expect(api.getTokenUsage).toHaveBeenCalledWith(42));
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    pushPoll();

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["token-usage", 42] });
  });

  it("does not invalidate token usage without an active agent", () => {
    renderHook(() => useTokenUsage(null, vi.fn()), { wrapper });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    pushPoll();

    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("invalidates pending messages for the active agent", async () => {
    renderHook(() => usePendingMessages(42, vi.fn()), { wrapper });
    await waitFor(() => expect(api.getPendingMessages).toHaveBeenCalledWith(42));
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    pushPoll();

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["pending", 42] });
  });

  it("does not invalidate pending messages without an active agent", () => {
    renderHook(() => usePendingMessages(null, vi.fn()), { wrapper });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    pushPoll();

    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});
