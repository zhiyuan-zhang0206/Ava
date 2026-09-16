// useDisplayLimit — runtime display-window defaults from GET /api/config
// (task #3696). The baked fallback must hold until/unless the config read
// lands: a failed, missing, or non-numeric field never blanks the value.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement } from "react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { useDisplayLimit } from "./display-limits";

vi.mock("./api", () => ({
  api: { getConfig: vi.fn() },
}));

let queryClient: QueryClient;

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

beforeEach(() => {
  vi.clearAllMocks();
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
});

afterEach(() => cleanup());

describe("useDisplayLimit", () => {
  it("returns the baked fallback while the config read is pending", () => {
    vi.mocked(api.getConfig).mockReturnValue(new Promise<never>(() => undefined));
    const { result } = renderHook(() => useDisplayLimit("AVA_X", 50), { wrapper });
    expect(result.current).toBe(50);
  });

  it("resolves the configured value by env var", async () => {
    vi.mocked(api.getConfig).mockResolvedValue({
      fields: [{ env_var: "AVA_X", current_value: 137 }],
    } as never);
    const { result } = renderHook(() => useDisplayLimit("AVA_X", 50), { wrapper });
    await waitFor(() => expect(result.current).toBe(137));
  });

  it("keeps the fallback when the field is missing", async () => {
    vi.mocked(api.getConfig).mockResolvedValue({
      fields: [{ env_var: "AVA_OTHER", current_value: 5 }],
    } as never);
    const { result } = renderHook(() => useDisplayLimit("AVA_X", 50), { wrapper });
    await waitFor(() => expect(api.getConfig).toHaveBeenCalled());
    expect(result.current).toBe(50);
  });

  it("keeps the fallback when the config read fails", async () => {
    vi.mocked(api.getConfig).mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useDisplayLimit("AVA_X", 50), { wrapper });
    await waitFor(() => expect(api.getConfig).toHaveBeenCalled());
    expect(result.current).toBe(50);
  });

  it("keeps the fallback when the payload is malformed (no fields list)", async () => {
    // The visual-regression e2e intercepts every /api/** request and fulfills
    // unknown endpoints with {} — a malformed payload must not crash the page
    // (user ruling: config unreachable -> baked fallback, never blank). The
    // act flush is load-bearing: the crash would happen when the resolved
    // data re-renders the hook, after the initial render returned.
    vi.mocked(api.getConfig).mockResolvedValue({} as never);
    const { result } = renderHook(() => useDisplayLimit("AVA_X", 50), { wrapper });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(result.current).toBe(50);
  });
});
