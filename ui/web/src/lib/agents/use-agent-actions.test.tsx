import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { ApiError, api } from "../transport/api";
import { AGENTS_QUERY_KEY, AGENT_DETAIL_QUERY_KEY } from "../fold/agents";
import { useStore } from "../state/store";
import { useAgentActions } from "./use-agent-actions";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  useStore.getState().setActiveId(null);
});

it("selects the committed agent after launch failure instead of retrying create", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const spawn = vi.spyOn(api, "spawnAgent").mockRejectedValue(new ApiError(502, "launch failed", {
    reason: "agent_launch_failed",
    agent_id: 123,
    state: { status: "idling", availability: { reason: "launch_unreachable" } },
    retry_launch_path: "/api/keyed/v1/agents/123/retry-launch",
  }));
  const showError = vi.fn();
  const wrapper = ({ children }: { children: ReactNode }) =>
    <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const { result } = renderHook(() => useAgentActions(showError, []), { wrapper });
  await act(async () => { expect(await result.current.spawn()).toBeNull(); });
  expect(spawn).toHaveBeenCalledTimes(1);
  expect(useStore.getState().activeId).toBe(123);
  expect(showError).toHaveBeenCalledWith(expect.stringContaining("Agent #123 was created"));
  expect(invalidate).toHaveBeenCalledWith({ queryKey: AGENTS_QUERY_KEY });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: [...AGENT_DETAIL_QUERY_KEY, 123] });
});


it("holds one creation key across mutation retries and gives concurrent actions distinct keys", async () => {
  const getRandomValues = crypto.getRandomValues.bind(crypto);
  vi.stubGlobal("crypto", { getRandomValues });
  const client = new QueryClient({ defaultOptions: { mutations: { retry: 1, retryDelay: 0 }, queries: { retry: false } } });
  const spawn = vi.spyOn(api, "spawnAgent").mockRejectedValueOnce(new Error("before send"))
    .mockResolvedValue({ id: 42 });
  const wrapper = ({ children }: { children: ReactNode }) =>
    <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const { result } = renderHook(() => useAgentActions(vi.fn(), []), { wrapper });
  await act(async () => { await result.current.spawn(); });
  expect(spawn.mock.calls[0][1]).toBe(spawn.mock.calls[1][1]);
  await act(async () => { await Promise.all([result.current.spawn(), result.current.spawn()]); });
  expect(spawn.mock.calls[2][1]).not.toBe(spawn.mock.calls[3][1]);
  client.clear();
});

it("retries compaction with its original observation and key", async () => {
  const target = {
    protocol: 1 as const, observation_id: "observed", source: {
      agent_id: 8, work_id: "work", machine: "local", generation: "generation", owner: "owner", protocol: 1 as const,
    },
    checkpoint_id: "source", checkpoint_ns: "", messages_version: "1",
    compact_channel_version: null, segment_version: 0, model: "gpt-5.6-sol",
  };
  const client = new QueryClient({ defaultOptions: { mutations: { retry: 1, retryDelay: 0 } } });
  const observe = vi.spyOn(api, "observeCompact").mockResolvedValue(target);
  const compact = vi.spyOn(api, "compact").mockRejectedValueOnce(new Error("response lost"))
    .mockResolvedValue({ target, command_id: "command" });
  const wrapper = ({ children }: { children: ReactNode }) =>
    <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const { result } = renderHook(() => useAgentActions(vi.fn(), []), { wrapper });
  await act(async () => { await result.current.compact(8); });
  expect(observe).toHaveBeenCalledTimes(1);
  expect(compact.mock.calls).toHaveLength(2);
  expect(compact.mock.calls[0]).toEqual(compact.mock.calls[1]);
  expect(compact.mock.calls[0][0]).toBe(target);
  await act(async () => { await result.current.compact(8); });
  expect(observe).toHaveBeenCalledTimes(2);
  expect(compact.mock.calls[2][1]).not.toBe(compact.mock.calls[0][1]);
  client.clear();
});
