// Sidebar preference hooks are DB-backed now (display.sidebar_*), so these tests
// drive them against the reactive user-settings mock and assert the read-side
// mapping: defaults, validation of stored values (sort shape,
// stats-window whitelist), and that a setter writes the right key. The legacy
// localStorage → DB migration is centralized and covered in
// settings-migration.test.ts.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { getStatsDashboard } = vi.hoisted(() => ({
  getStatsDashboard: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api: { getStatsDashboard } }));
vi.mock("@/lib/use-user-settings", () => import("@/test-support/user-settings-mock"));

import { mockSetSettingCalls, resetMockSettings, setMockSetting } from "@/test-support/user-settings-mock";

import {
  SIDEBAR_SORT_DEFAULT,
  STATS_WINDOW_DEFAULT,
  useSidebarCollapsed,
  useSidebarSort,
  useStatsDashboard,
  useStatsWindow,
} from "./sidebar";

beforeEach(() => resetMockSettings());
afterEach(() => {
  cleanup();
  getStatsDashboard.mockReset();
  vi.useRealTimers();
});

describe("useSidebarSort (DB-backed)", () => {
  it("default sort is id descending", () => {
    const { result } = renderHook(() => useSidebarSort());
    expect(result.current.sort).toEqual(SIDEBAR_SORT_DEFAULT);
    expect(SIDEBAR_SORT_DEFAULT).toEqual({ key: "id", dir: "desc" });
  });

  it("reads a stored valid sort", () => {
    setMockSetting("display.sidebar_sort", { key: "status", dir: "asc" });
    const { result } = renderHook(() => useSidebarSort());
    expect(result.current.sort).toEqual({ key: "status", dir: "asc" });
  });

  it("malformed stored value (not an object) → default kept", () => {
    setMockSetting("display.sidebar_sort", "not-an-object");
    const { result } = renderHook(() => useSidebarSort());
    expect(result.current.sort).toEqual(SIDEBAR_SORT_DEFAULT);
  });

  it("out-of-range key/dir → default kept", () => {
    setMockSetting("display.sidebar_sort", { key: "bogus", dir: "sideways" });
    const { result } = renderHook(() => useSidebarSort());
    expect(result.current.sort).toEqual(SIDEBAR_SORT_DEFAULT);
  });

  it("setSort writes display.sidebar_sort", () => {
    const { result } = renderHook(() => useSidebarSort());
    act(() => result.current.setSort({ key: "last_active", dir: "desc" }));
    expect(result.current.sort).toEqual({ key: "last_active", dir: "desc" });
    expect(mockSetSettingCalls().at(-1)).toEqual({
      key: "display.sidebar_sort",
      value: { key: "last_active", dir: "desc" },
    });
  });
});

describe("useSidebarCollapsed", () => {
  it("setCollapsed writes display.sidebar_collapsed", () => {
    const { result } = renderHook(() => useSidebarCollapsed());
    act(() => result.current.setCollapsed(true));
    expect(mockSetSettingCalls().at(-1)).toEqual({
      key: "display.sidebar_collapsed",
      value: true,
    });
  });
});

describe("useStatsWindow (DB-backed, validated)", () => {
  it("keeps the valid five-minute window", () => {
    setMockSetting("display.stats_window_hours", 0);
    const { result } = renderHook(() => useStatsWindow());
    expect(result.current.windowHours).toBe(0);
  });

  it("rejects an out-of-whitelist window → default", () => {
    setMockSetting("display.stats_window_hours", 999);
    const { result } = renderHook(() => useStatsWindow());
    expect(result.current.windowHours).toBe(STATS_WINDOW_DEFAULT);
  });
});

describe("useStatsDashboard shared polling", () => {
  it("uses one 30s cadence for staggered observers of the same page cache", async () => {
    vi.useFakeTimers();
    getStatsDashboard.mockResolvedValue({
      live_count: 1,
      window_hours: 24,
      tokens: { input: 0, output: 0, cache_read: 0, cache_hit_pct: 0 },
      cost_usd: 0,
      avg_turn_seconds: null,
      warnings: 0,
      errors: 0,
      total_events: 0,
    });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);

    const first = renderHook(() => useStatsDashboard(24), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(getStatsDashboard).toHaveBeenCalledTimes(1);
    expect(getStatsDashboard).toHaveBeenNthCalledWith(1, 24, expect.any(AbortSignal));

    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    const second = renderHook(() => useStatsDashboard(24), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(getStatsDashboard).toHaveBeenCalledTimes(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(getStatsDashboard).toHaveBeenCalledTimes(2);
    expect(getStatsDashboard).toHaveBeenNthCalledWith(2, 24);
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(getStatsDashboard).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(getStatsDashboard).toHaveBeenCalledTimes(3);

    first.unmount();
    second.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(getStatsDashboard).toHaveBeenCalledTimes(3);
  });

  it("retries one failed request and exposes the error", async () => {
    const failure = new Error("stats endpoint 500");
    getStatsDashboard.mockRejectedValue(failure);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retryDelay: 0 } },
    });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);

    const hook = renderHook(() => useStatsDashboard(24), { wrapper });
    await waitFor(() => expect(hook.result.current.error).toBe(failure));

    expect(getStatsDashboard).toHaveBeenCalledTimes(2);
    expect(hook.result.current.isFetching).toBe(false);
    hook.unmount();
  });
});
