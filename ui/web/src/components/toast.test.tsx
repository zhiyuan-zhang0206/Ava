"use client";

// ToastHost — the root-level toast renderer (Task #1051): mounted in
// Providers so error toasts reach the user on EVERY route, not just Home,
// and announced via role="alert" (a11y: errors visible to every perception
// channel).

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ToastHost } from "./toast";
import { useStore } from "@/lib/store";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  act(() => useStore.setState({ toast: null }));
});

describe("ToastHost", () => {
  it("renders nothing while the toast slot is empty", () => {
    render(<ToastHost />);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders the store toast with role=alert", () => {
    vi.useFakeTimers();
    render(<ToastHost />);
    act(() => useStore.getState().showToast("Save failed: 500"));
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toBe("Save failed: 500");
  });

  it("dismisses after 3s", () => {
    vi.useFakeTimers();
    render(<ToastHost />);
    act(() => useStore.getState().showToast("boom"));
    expect(screen.getByRole("alert")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(3_000);
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
