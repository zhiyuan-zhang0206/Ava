// UpdatingPage tests — the full-screen "System updating..." page.
//
// Verifies:
//   - Renders the updating message with a spinner
//   - Shows elapsed time counter
//   - Does not probe auth or reload; AuthGuard's cluster state owns recovery

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

  it("does not probe auth or reload while cluster state owns recovery", async () => {
    checkAuthMock.mockResolvedValue({ authenticated: true });
    render(<UpdatingPage />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(checkAuthMock).not.toHaveBeenCalled();
    expect(reloadMock).not.toHaveBeenCalled();
  });
});
