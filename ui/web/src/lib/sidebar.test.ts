// Sidebar preference hooks are DB-backed now (display.sidebar_*), so these tests
// drive them against the reactive user-settings mock and assert the read-side
// mapping: defaults, validation of stored values (sort shape, width clamp,
// stats-window whitelist), and that a setter writes the right key. The legacy
// localStorage → DB migration is centralized and covered in
// settings-migration.test.ts.

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/use-user-settings", () => import("@/test-support/user-settings-mock"));

import { mockSetSettingCalls, resetMockSettings, setMockSetting } from "@/test-support/user-settings-mock";

import {
  SIDEBAR_SORT_DEFAULT,
  SIDEBAR_WIDTH,
  STATS_WINDOW_DEFAULT,
  useSidebarSort,
  useSidebarWidth,
  useStatsWindow,
} from "./sidebar";

beforeEach(() => resetMockSettings());
afterEach(cleanup);

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

describe("useSidebarWidth (fixed width, task #750)", () => {
  it("always returns the fixed width, ignoring any persisted legacy value", () => {
    // A legacy display.sidebar_width (from the drag-resize era) must not
    // influence the width — the sidebar is fixed now.
    setMockSetting("display.sidebar_width", 300);
    const { result } = renderHook(() => useSidebarWidth());
    expect(result.current.width).toBe(SIDEBAR_WIDTH);
  });

  it("does not expose setWidth (no drag resize)", () => {
    const { result } = renderHook(() => useSidebarWidth());
    expect("setWidth" in result.current).toBe(false);
  });

  it("setCollapsed writes display.sidebar_collapsed", () => {
    const { result } = renderHook(() => useSidebarWidth());
    act(() => result.current.setCollapsed(true));
    expect(mockSetSettingCalls().at(-1)).toEqual({
      key: "display.sidebar_collapsed",
      value: true,
    });
  });
});

describe("useStatsWindow (DB-backed, validated)", () => {
  it("keeps a valid whitelisted window", () => {
    setMockSetting("display.stats_window_hours", 72);
    const { result } = renderHook(() => useStatsWindow());
    expect(result.current.windowHours).toBe(72);
  });

  it("rejects an out-of-whitelist window → default", () => {
    setMockSetting("display.stats_window_hours", 999);
    const { result } = renderHook(() => useStatsWindow());
    expect(result.current.windowHours).toBe(STATS_WINDOW_DEFAULT);
  });
});
