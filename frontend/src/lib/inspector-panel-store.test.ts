// useInspectorOpen — the inspector open/closed flag is breakpoint-aware:
//
// - Desktop (≥ lg): a workspace preference (DB-backed display.inspector_open),
//   default OPEN (the panel is a side panel beside the timeline — #723).
// - Mobile (< lg): per-session view state (zustand, like the mobile sidebar
//   drawer), default CLOSED — the panel is a full-screen overlay that hides
//   the timeline, so first load must land on the timeline (task #793).
//   Mobile toggles NEVER write the shared setting: opening the inspector on a
//   phone must not yank the desktop panel open, and closing it there must not
//   close the desktop one.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useInspectorOpen } from "./inspector-panel-store";
import { useStore } from "./store";

const { isLargeMock, settings, setSettingMock } = vi.hoisted(() => {
  // Stateful settings map: setSetting mutates it, mirroring the real
  // useUserSettings optimistic-update behavior, so a toggle flips `open` on
  // the next render just like in the app.
  const settings: Record<string, unknown> = {};
  const setSettingMock = vi.fn((key: string, value: unknown) => {
    settings[key] = value;
  });
  return { isLargeMock: vi.fn<() => boolean>(() => true), settings, setSettingMock };
});

vi.mock("./breakpoint", () => ({
  useBreakpoint: () => ({
    tier: isLargeMock() ? "xl" : "xs",
    isNarrow: !isLargeMock(),
    isLarge: isLargeMock(),
  }),
}));
vi.mock("./use-user-settings", () => ({
  useUserSettings: () => ({
    settings,
    setSetting: setSettingMock,
    isLoading: false,
  }),
}));

beforeEach(() => {
  isLargeMock.mockReturnValue(true);
  // Reset the shared settings map to empty (no dynamic delete — assign a
  // fresh object; the mocked hook reads it by reference each render).
  Object.keys(settings).forEach((k) => {
    settings[k] = undefined;
  });
  setSettingMock.mockClear();
  useStore.setState({ mobileInspectorOpen: false });
});

afterEach(() => {
  useStore.setState({ mobileInspectorOpen: false });
});

describe("useInspectorOpen — desktop (≥ lg)", () => {
  it("defaults to CLOSED when the setting is unset (floating panel, #835)", () => {
    const { result } = renderHook(() => useInspectorOpen());
    expect(result.current.open).toBe(false);
  });

  it("is open only when the setting is explicitly true", () => {
    settings["display.inspector_open"] = true;
    const { result } = renderHook(() => useInspectorOpen());
    expect(result.current.open).toBe(true);
  });

  it("toggle writes the shared setting and never touches the mobile store", () => {
    settings["display.inspector_open"] = false;
    const { result, rerender } = renderHook(() => useInspectorOpen());
    expect(result.current.open).toBe(false);

    act(() => result.current.toggle());
    rerender(); // adopt the mutated settings — the real hook re-renders off the settings cache
    expect(result.current.open).toBe(true);
    expect(setSettingMock).toHaveBeenCalledWith("display.inspector_open", true);
    expect(useStore.getState().mobileInspectorOpen).toBe(false);

    act(() => result.current.toggle());
    rerender();
    expect(result.current.open).toBe(false);
    expect(setSettingMock).toHaveBeenLastCalledWith("display.inspector_open", false);
  });
});

describe("useInspectorOpen — mobile (< lg)", () => {
  beforeEach(() => {
    isLargeMock.mockReturnValue(false);
  });

  it("defaults to closed — the overlay must not hide the timeline on first load", () => {
    // Even with the workspace setting at its default (open), mobile starts
    // on the timeline.
    const { result } = renderHook(() => useInspectorOpen());
    expect(result.current.open).toBe(false);
  });

  it("opens when toggled and closes when toggled again — session state only", () => {
    const { result } = renderHook(() => useInspectorOpen());
    expect(result.current.open).toBe(false);

    act(() => result.current.toggle());
    expect(result.current.open).toBe(true);
    expect(useStore.getState().mobileInspectorOpen).toBe(true);

    act(() => result.current.toggle());
    expect(result.current.open).toBe(false);
    expect(useStore.getState().mobileInspectorOpen).toBe(false);
  });

  it("mobile toggle never writes the shared desktop setting", () => {
    const { result } = renderHook(() => useInspectorOpen());
    act(() => result.current.toggle());
    act(() => result.current.toggle());
    expect(setSettingMock).not.toHaveBeenCalled();
  });

  it("an explicit desktop preference does not force the overlay open on mobile", () => {
    // The workspace setting says open — but on mobile that only means the
    // desktop panel; the overlay stays closed until the user opens it here.
    settings["display.inspector_open"] = true;
    const { result } = renderHook(() => useInspectorOpen());
    expect(result.current.open).toBe(false);
  });
});

describe("useInspectorOpen — breakpoint switch", () => {
  it("flips between the two sources without cross-talk", () => {
    settings["display.inspector_open"] = false;
    const { result, rerender } = renderHook(() => useInspectorOpen());
    // Desktop: closed (explicit false).
    expect(result.current.open).toBe(false);

    // → Mobile: session state (closed), desktop setting ignored.
    isLargeMock.mockReturnValue(false);
    rerender();
    expect(result.current.open).toBe(false);
    act(() => result.current.toggle());
    expect(result.current.open).toBe(true);

    // → Desktop: back to the workspace setting, mobile session state ignored.
    isLargeMock.mockReturnValue(true);
    rerender();
    expect(result.current.open).toBe(false);
  });
});
