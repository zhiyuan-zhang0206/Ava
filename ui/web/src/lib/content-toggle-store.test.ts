// useContentToggle — a thin, DB-backed wrapper over useUserSettings
// (display.expand_runs_mode). These tests drive it against the reactive
// user-settings mock and assert it reads the stored value, defaults to "all",
// and writes the right key on setDetailsMode.

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/use-user-settings", () => import("@/test-support/user-settings-mock"));

import { mockSetSettingCalls, resetMockSettings, setMockSetting } from "@/test-support/user-settings-mock";

import { useContentToggle } from "./content-toggle-store";

beforeEach(() => resetMockSettings());
afterEach(cleanup);

describe("useContentToggle", () => {
  it('no stored value → detailsMode defaults to "all"', () => {
    const { result } = renderHook(() => useContentToggle());
    expect(result.current.detailsMode).toBe("all");
  });

  it("surfaces isLoading from the settings query (consumers gate expansion on it)", () => {
    const { result } = renderHook(() => useContentToggle());
    // The mock is synchronous (never loading); the contract is that the field
    // exists and is false once settings are known.
    expect(result.current.isLoading).toBe(false);
  });

  it('detailsMode reads from display.expand_runs_mode', () => {
    setMockSetting("display.expand_runs_mode", "none");
    const { result } = renderHook(() => useContentToggle());
    expect(result.current.detailsMode).toBe("none");
  });

  it('legacy "auto" → "last"', () => {
    setMockSetting("display.expand_runs_mode", "auto");
    const { result } = renderHook(() => useContentToggle());
    expect(result.current.detailsMode).toBe("last");
  });

  it("legacy true → all", () => {
    setMockSetting("display.expand_runs_mode", true);
    const { result } = renderHook(() => useContentToggle());
    expect(result.current.detailsMode).toBe("all");
  });

  it("legacy false → none", () => {
    setMockSetting("display.expand_runs_mode", false);
    const { result } = renderHook(() => useContentToggle());
    expect(result.current.detailsMode).toBe("none");
  });

  it('setDetailsMode writes display.expand_runs_mode="last"', () => {
    const { result } = renderHook(() => useContentToggle());
    act(() => result.current.setDetailsMode("last"));
    expect(mockSetSettingCalls().at(-1)).toEqual({
      key: "display.expand_runs_mode",
      value: "last",
    });
  });
});
