import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { ApiError, api } from "./api";
import { AGENTS_QUERY_KEY, AGENT_DETAIL_QUERY_KEY } from "./fold/agents";
import { useStore } from "./store";
import { useAgentActions } from "./use-agent-actions";

afterEach(() => {
  vi.restoreAllMocks();
  useStore.getState().setActiveId(null);
});

it("selects the committed agent after launch failure instead of retrying create", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const spawn = vi.spyOn(api, "spawnAgent").mockRejectedValue(new ApiError(502, "launch failed", {
    reason: "agent_launch_failed",
    agent_id: 123,
    state: { status: "idling", availability: { reason: "launch_unreachable" } },
    retry_launch_path: "/api/agents/123/retry-launch",
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
