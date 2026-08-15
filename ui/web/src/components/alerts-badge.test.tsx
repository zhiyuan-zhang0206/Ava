// The header-bar alerts badge: the unread count from the ["alerts"] cache;
// renders nothing at zero.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { AlertsBadge } from "@/components/alerts-badge";
import type { AlertsResponse } from "@/lib/types";

afterEach(cleanup);

function response(unread: number): AlertsResponse {
  return {
    alerts: [],
    meta: { window: "24h", include_read: false, total: 0, unresolved_count: 0, unread_count: unread },
  };
}

function wrap(ui: React.ReactElement, seed: AlertsResponse | null) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  if (seed) qc.setQueryData(["alerts"], seed);
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("AlertsBadge", () => {
  it("shows the unread count and links to the alert section", () => {
    wrap(<AlertsBadge />, response(5));
    const badge = screen.getByTestId("alerts-badge");
    expect(badge.getAttribute("href")).toBe("/insights#alerts");
    expect(screen.getByTestId("alerts-badge-count").textContent).toBe("5");
  });

  it("renders nothing at zero unread", () => {
    wrap(<AlertsBadge />, response(0));
    expect(screen.queryByTestId("alerts-badge")).toBeNull();
  });

  it("caps the badge at 99+", () => {
    wrap(<AlertsBadge />, response(250));
    expect(screen.getByTestId("alerts-badge-count").textContent).toBe("99+");
  });
});
