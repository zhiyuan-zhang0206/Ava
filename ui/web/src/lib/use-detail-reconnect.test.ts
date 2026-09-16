import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, useEffect } from "react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { usePendingMessages } from "./use-pending-messages";
import { useTokenUsage } from "./use-token-usage";
import type { SystemEvent, TokenUsageResponse } from "./types";
import type { ConnectionEvent } from "./useEventStream";

vi.mock("./api", () => ({
  api: { getPendingMessages: vi.fn(), getTokenUsage: vi.fn() },
}));

const connectionHandlers = new Set<(event: ConnectionEvent) => void>();
const systemHandlers = new Set<(event: SystemEvent) => void>();
vi.mock("./useEventStream", () => ({
  useAgentEventStream: (
    onEvent: (event: SystemEvent) => void,
    onConnectionEvent: (event: ConnectionEvent) => void,
  ) => {
    useEffect(() => {
      connectionHandlers.add(onConnectionEvent);
      systemHandlers.add(onEvent);
      return () => { connectionHandlers.delete(onConnectionEvent); systemHandlers.delete(onEvent); };
    }, [onEvent, onConnectionEvent]);
  },
}));

function pushOpen(): void {
  act(() => {
    for (const handler of connectionHandlers) handler({ type: "open" });
  });
}

type Domain = "token-usage" | "pending";
interface Read {
  id: number;
  domain: Domain;
  signal: AbortSignal | undefined;
  finish: (value: number) => void;
}
let reads: Read[];
let queryClient: QueryClient;
const showError = vi.fn();

function tokenSnapshot(input: number): TokenUsageResponse {
  return { input_tokens: input, output_tokens: 0, reasoning_tokens: 0,
    max_input_tokens: 100_000, soft_compact_tokens: 70_000, hard_compact_tokens: 90_000 };
}

function useRead(domain: Domain, id: number | null) {
  const token = useTokenUsage(domain === "token-usage" ? id : null, showError);
  const pending = usePendingMessages(domain === "pending" ? id : null, showError);
  return domain === "token-usage" ? token.contextTokens : Number(pending[0]?.content ?? 0);
}

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

beforeEach(() => {
  vi.clearAllMocks();
  reads = [];
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(api.getTokenUsage).mockImplementation((id, signal) => new Promise((resolve) => {
    reads.push({ id, signal, domain: "token-usage", finish: (value) => resolve(tokenSnapshot(value)) });
  }));
  vi.mocked(api.getPendingMessages).mockImplementation((id, signal) => new Promise((resolve) => {
    reads.push({ id, signal, domain: "pending", finish: (value) => resolve([
      { id: value, content: String(value), source: "user", images: null, created_at: "2026-09-16T00:00:00Z" },
    ]) });
  }));
});

afterEach(() => {
  cleanup();
  queryClient.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe.each<Domain>(["token-usage", "pending"])("%s selected read ownership", (domain) => {
  it("repairs an initial opening gap and another reconnect during that repair", async () => {
    const { result } = renderHook(() => useRead(domain, 42), { wrapper });
    await waitFor(() => expect(reads).toHaveLength(1));
    pushOpen();
    pushOpen();
    expect(reads).toHaveLength(1);
    await act(async () => { reads[0].finish(1); await Promise.resolve(); });
    await waitFor(() => expect(reads).toHaveLength(2));
    pushOpen();
    await act(async () => { reads[1].finish(2); await Promise.resolve(); });
    await waitFor(() => expect(reads).toHaveLength(3));
    await act(async () => { reads[2].finish(3); await Promise.resolve(); });
    await waitFor(() => expect(result.current).toBe(3));
  });

  it("aborts obsolete A-to-B-to-A requests and rejects their late values", async () => {
    const { result, rerender } = renderHook<number, { id: number | null }>(({ id }) => useRead(domain, id), {
      initialProps: { id: 1 }, wrapper,
    });
    await waitFor(() => expect(reads).toHaveLength(1));
    rerender({ id: 2 });
    await waitFor(() => expect(reads).toHaveLength(2));
    expect(reads[0].signal?.aborted).toBe(true);
    rerender({ id: 1 });
    await waitFor(() => expect(reads).toHaveLength(3));
    expect(reads[1].signal?.aborted).toBe(true);
    await act(async () => { reads[2].finish(3); await Promise.resolve(); });
    await waitFor(() => expect(result.current).toBe(3));
    await act(async () => { reads[0].finish(1); reads[1].finish(2); await Promise.resolve(); });
    expect(result.current).toBe(3);
    expect(showError).not.toHaveBeenCalled();
  });

  it.each(["hidden", "unmount", "selection"])("%s aborts reads and disposes trailing work", async (cause) => {
    const view = renderHook<number, { id: number | null }>(({ id }) => useRead(domain, id), {
      initialProps: { id: 1 }, wrapper,
    });
    await waitFor(() => expect(reads).toHaveLength(1));
    pushOpen();
    vi.useFakeTimers();
    if (cause === "hidden") {
      vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
      act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    } else if (cause === "unmount") {
      view.unmount();
    } else {
      view.rerender({ id: null });
    }
    expect(reads[0].signal?.aborted).toBe(true);
    await act(async () => {
      reads[0].finish(9);
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(reads).toHaveLength(1);
    expect(showError).not.toHaveBeenCalled();
  });

  it("does no read or repair without selection or while initially hidden", () => {
    const view = renderHook<number, { id: number | null }>(({ id }) => useRead(domain, id), {
      initialProps: { id: null }, wrapper,
    });
    pushOpen();
    expect(reads).toHaveLength(0);
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    view.rerender({ id: 1 });
    pushOpen();
    expect(reads).toHaveLength(0);
  });
});


it("pending turn-start bursts cannot postpone repair and leave a trailing read", async () => {
  renderHook(() => useRead("pending", 42), { wrapper });
  await waitFor(() => expect(reads).toHaveLength(1));
  await act(async () => { reads[0].finish(1); await Promise.resolve(); });
  await waitFor(() => expect(queryClient.isFetching()).toBe(0));
  vi.useFakeTimers();
  const start = () => {
    act(() => {
      for (const handler of systemHandlers) handler({ role: "chat_start", agent_id: 42, item_id: "1.0" });
    });
  };
  start();
  await act(async () => { await vi.advanceTimersByTimeAsync(150); });
  start();
  await act(async () => { await vi.advanceTimersByTimeAsync(50); });
  expect(reads).toHaveLength(2);
  start();
  await act(async () => {
    reads[1].finish(2);
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(reads).toHaveLength(3);
});
