// The timeline floating bar: shows while unresolved > 0, auto-hides ~3 min
// after the last count increase, and links to /insights#alerts. The unread
// badge shares the same cache (["alerts"]).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AlertsFloatingBar } from "@/components/alerts-floating-bar";
import type { AlertsResponse } from "@/lib/types";

afterEach(cleanup);

function response(unresolved: number): AlertsResponse {
  return {
    alerts: [],
    meta: { window: "24h", include_read: false, total: 0, unresolved_count: unresolved, unread_count: 0 },
  };
}

function wrap(ui: React.ReactElement, seed: AlertsResponse | null) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  if (seed) qc.setQueryData(["alerts"], seed);
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("AlertsFloatingBar", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the unresolved count and links to the alert section", () => {
    wrap(<AlertsFloatingBar />, response(3));
    const bar = screen.getByTestId("alerts-floating-bar");
    expect(bar.textContent).toContain("3");
    expect(bar.getAttribute("href")).toBe("/insights#alerts");
  });

  it("renders nothing when there is nothing unresolved", () => {
    wrap(<AlertsFloatingBar />, response(0));
    expect(screen.queryByTestId("alerts-floating-bar")).toBeNull();
  });

  it("auto-hides ~3 minutes after the last increase", async () => {
    wrap(<AlertsFloatingBar />, response(2));
    expect(screen.getByTestId("alerts-floating-bar")).toBeTruthy();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3 * 60 * 1000 + 1_000);
    });
    expect(screen.queryByTestId("alerts-floating-bar")).toBeNull();
  });
});
