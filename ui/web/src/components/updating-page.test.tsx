// UpdatingPage tests — the full-screen "System updating..." page.
//
// Verifies:
//   - Renders the updating message with a spinner
//   - Shows elapsed time counter
//   - Polls auth endpoint and reloads on success

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock api.checkAuth — vi.hoisted ensures the mock is available before vi.mock is hoisted
const { checkAuthMock } = vi.hoisted(() => ({
  checkAuthMock: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  api: { checkAuth: checkAuthMock },
}));

// Capture reload calls
const reloadMock = vi.fn();
const originalLocation = window.location;
beforeEach(() => {
  // Use defineProperty to mock reload since it's not configurable
  Object.defineProperty(window, "location", {
    value: { reload: reloadMock },
    writable: true,
    configurable: true,
  });
});

import { UpdatingPage } from "./updating-page";

beforeEach(() => {
  checkAuthMock.mockReset();
  reloadMock.mockReset();
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  Object.defineProperty(window, "location", {
    value: originalLocation,
    writable: true,
    configurable: true,
  });
});

describe("UpdatingPage", () => {
  it("renders the updating message with spinner", () => {
    checkAuthMock.mockRejectedValue(new Error("unreachable"));
    render(<UpdatingPage />);
    expect(screen.getByText("System updating")).toBeTruthy();
    expect(screen.getByText(/reconnecting automatically/)).toBeTruthy();
    expect(document.querySelector(".animate-spin")).not.toBeNull();
  });

  it("shows initial elapsed time", () => {
    checkAuthMock.mockRejectedValue(new Error("unreachable"));
    render(<UpdatingPage />);
    // Initial state shows Waiting 0s
    expect(screen.getByText(/Waiting 0s/)).toBeTruthy();
  });

  it("polls auth immediately on mount", () => {
    checkAuthMock.mockRejectedValue(new Error("unreachable"));
    render(<UpdatingPage />);
    // Immediate call on mount
    expect(checkAuthMock).toHaveBeenCalledTimes(1);
  });

  it("polls auth every 5 seconds", async () => {
    checkAuthMock.mockRejectedValue(new Error("unreachable"));
    render(<UpdatingPage />);
    expect(checkAuthMock).toHaveBeenCalledTimes(1);
    // Let the mount check settle so the in-flight guard releases before the
    // first tick (the guard is the point of the fix — a hung request must
    // not stack).
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    // Advance by 5s
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(checkAuthMock).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    // Advance by another 5s
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(checkAuthMock).toHaveBeenCalledTimes(3);
  });

  it("does not stack a new checkAuth while one is still in flight (Task #1051)", async () => {
    let resolveCheck!: (v: { authenticated: boolean }) => void;
    checkAuthMock.mockImplementation(
      () => new Promise<{ authenticated: boolean }>((resolve) => { resolveCheck = resolve; }),
    );
    render(<UpdatingPage />);
    expect(checkAuthMock).toHaveBeenCalledTimes(1);
    // A hung request (half-dead backend accepts the connection but never
    // answers): three ticks pass, no stacked requests.
    act(() => {
      vi.advanceTimersByTime(15_000);
    });
    expect(checkAuthMock).toHaveBeenCalledTimes(1);
    // The hung request resolves → the next tick fires a fresh check.
    await act(async () => {
      resolveCheck({ authenticated: false });
      await vi.advanceTimersByTimeAsync(0);
    });
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(checkAuthMock).toHaveBeenCalledTimes(2);
  });

  it("reloads the page when auth succeeds", async () => {
    // First call: still down
    checkAuthMock.mockRejectedValueOnce(new Error("unreachable"));
    render(<UpdatingPage />);
    expect(reloadMock).not.toHaveBeenCalled();

    // Next poll: backend is back — resolve with authenticated: true
    checkAuthMock.mockResolvedValueOnce({ authenticated: true });
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    // Wait for the promise to resolve
    await vi.runOnlyPendingTimersAsync();
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });

  it("reloads when auth answers not-authenticated (expired session)", async () => {
    // A successful not-authenticated answer means the gateway is serving
    // again (a paused gateway 503s /api/auth/check) with the session expired
    // — e.g. across a host crash. The reload lands on the login page;
    // waiting for `authenticated: true` would spin here forever.
    checkAuthMock.mockRejectedValueOnce(new Error("unreachable"));
    render(<UpdatingPage />);
    checkAuthMock.mockResolvedValueOnce({ authenticated: false });
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    await vi.runOnlyPendingTimersAsync();
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });
});
