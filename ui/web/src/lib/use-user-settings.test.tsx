// useUserSettings hook tests — query loading, optimistic update, error rollback.

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { useDebouncedSetting, useUserSettings } from "@/lib/use-user-settings";

vi.mock("@/lib/api", () => ({
  api: {
    getSettings: vi.fn(),
    putSetting: vi.fn(),
  },
}));

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(cleanup);

describe("useUserSettings", () => {
  it("returns defaults when server returns empty", async () => {
    vi.spyOn(api, "getSettings").mockResolvedValue({ settings: [] });

    const { result } = renderHook(() => useUserSettings(), { wrapper });

    await waitFor(() => !result.current.isLoading);

    expect(result.current.settings["display.show_machine_name"]).toBe(true);
    expect(result.current.settings["display.time_mode"]).toBe("last_active");
  });

  it("merges server values over defaults", async () => {
    vi.spyOn(api, "getSettings").mockResolvedValue({
      settings: [
        { key: "display.show_machine_name", value: false, updated_at: "2026-01-01T00:00:00Z" },
      ],
    });

    const { result } = renderHook(() => useUserSettings(), { wrapper });

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.settings["display.show_machine_name"]).toBe(false);
    expect(result.current.settings["display.time_mode"]).toBe("last_active");
  });


  it("setSetting rolls back on server error", async () => {
    vi.spyOn(api, "getSettings").mockResolvedValue({ settings: [] });
    vi.spyOn(api, "putSetting").mockRejectedValue(new Error("Server error"));

    const { result } = renderHook(() => useUserSettings(), { wrapper });

    await waitFor(() => !result.current.isLoading);

    // Initial value from defaults
    expect(result.current.settings["display.show_machine_name"]).toBe(true);

    // Attempt to update — should fail and roll back
    result.current.setSetting("display.show_machine_name", false);

    // After error, value should still be the default (rolled back)
    await waitFor(() => {
      expect(result.current.settings["display.show_machine_name"]).toBe(true);
    });
  });



  it("rolls back to defaults when the PUT fails before the initial GET resolves", async () => {
    // Cache is still empty (GET pending) — e.g. the sidebar quick toggle
    // clicked right after load. The rollback snapshot must fall back to the
    // defaults instead of silently skipping the rollback.
    vi.spyOn(api, "getSettings").mockReturnValue(new Promise(() => undefined));
    let rejectPut: (e: Error) => void = () => undefined;
    vi.spyOn(api, "putSetting").mockReturnValue(
      new Promise((_, reject) => {
        rejectPut = reject;
      }),
    );

    const { result } = renderHook(() => useUserSettings(), { wrapper });

    // Default (quiet) value before the toggle.
    expect(result.current.settings["display.show_agent_status"]).toBe(false);

    result.current.setSetting("display.show_agent_status", true);

    // Optimistic flip lands even with the GET unresolved…
    await waitFor(() => {
      expect(result.current.settings["display.show_agent_status"]).toBe(true);
    });

    // …and the failed PUT rolls it back to the default, not a stuck true.
    rejectPut(new Error("Server error"));
    await waitFor(() => {
      expect(result.current.settings["display.show_agent_status"]).toBe(false);
    });
  });

  it("caches settings and does not refetch on every call", async () => {
    const spy = vi.spyOn(api, "getSettings").mockResolvedValue({ settings: [] });

    // First render
    const { result, rerender } = renderHook(() => useUserSettings(), { wrapper });
    await waitFor(() => !result.current.isLoading);
    expect(spy).toHaveBeenCalledTimes(1);

    // Rerender — should not refetch (staleTime = 60s)
    rerender();
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("setSetting optimistically updates and calls PUT", async () => {
    vi.spyOn(api, "getSettings").mockResolvedValue({ settings: [] });
    vi.spyOn(api, "putSetting").mockResolvedValue({
      key: "display.show_machine_name",
      value: false,
      updated_at: "2026-01-01T00:00:00Z",
    });

    const { result } = renderHook(() => useUserSettings(), { wrapper });

    await waitFor(() => !result.current.isLoading);

    result.current.setSetting("display.show_machine_name", false);

    await waitFor(() => {
      expect(api.putSetting).toHaveBeenCalledWith("display.show_machine_name", false);
    });
  });
});

// useDebouncedSetting — the write path for dragged DB settings (timeline
// width, force-layout sliders): local value live immediately, DB PUT
// coalesced, and a pending write flushed on unmount.
describe("useDebouncedSetting", () => {
  beforeEach(() => {
    vi.spyOn(api, "getSettings").mockResolvedValue({ settings: [] });
    vi.spyOn(api, "putSetting").mockResolvedValue({
      key: "display.timeline_width_ratio",
      value: 0.4,
      updated_at: "2026-01-01T00:00:00Z",
    });
  });

  it("updates the local value immediately but debounces the PUT", async () => {
    const { result } = renderHook(
      () => useDebouncedSetting("display.timeline_width_ratio", 0.4, 50),
      { wrapper },
    );
    await waitFor(() => expect(result.current[0]).toBe(0.4));

    act(() => result.current[1](0.5));
    // Local value is live immediately (for a smooth drag)…
    expect(result.current[0]).toBe(0.5);
    // …but the network write has not fired yet.
    expect(api.putSetting).not.toHaveBeenCalled();

    // After the debounce window, exactly one PUT lands with the final value.
    await waitFor(() =>
      expect(api.putSetting).toHaveBeenCalledWith("display.timeline_width_ratio", 0.5),
    );
    expect(api.putSetting).toHaveBeenCalledTimes(1);
  });

  it("coalesces a rapid burst of changes into one trailing PUT", async () => {
    const { result } = renderHook(
      () => useDebouncedSetting("display.timeline_width_ratio", 0.4, 50),
      { wrapper },
    );
    await waitFor(() => expect(result.current[0]).toBe(0.4));

    act(() => result.current[1](0.45));
    act(() => result.current[1](0.48));
    act(() => result.current[1](0.5));

    await waitFor(() =>
      expect(api.putSetting).toHaveBeenCalledWith("display.timeline_width_ratio", 0.5),
    );
    expect(api.putSetting).toHaveBeenCalledTimes(1);
  });

  it("flushes a pending write on unmount", async () => {
    const { result, unmount } = renderHook(
      // Long delay so the timer never fires on its own — only the unmount flush can.
      () => useDebouncedSetting("display.timeline_width_ratio", 0.4, 10_000),
      { wrapper },
    );
    await waitFor(() => expect(result.current[0]).toBe(0.4));

    act(() => result.current[1](0.6));
    expect(api.putSetting).not.toHaveBeenCalled();

    unmount();
    await waitFor(() =>
      expect(api.putSetting).toHaveBeenCalledWith("display.timeline_width_ratio", 0.6),
    );
  });
});
