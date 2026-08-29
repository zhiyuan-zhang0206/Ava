// The alert section (/insights#alerts) — unresolved-first history rendered
// from the ["alerts", "section"] cache. Alert is separate from Notice.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AlertsSection from "@/components/ops/alerts-section";
import { api } from "@/lib/api";
import type { AlertsResponse } from "@/lib/types";

afterEach(cleanup);

beforeEach(() => {
  vi.restoreAllMocks();
});

function response(overrides: Partial<AlertsResponse> = {}): AlertsResponse {
  return {
    alerts: [],
    meta: {
      window: "24h",
      total: 0,
      unresolved_count: 0,
    },
    ...overrides,
  };
}

const ROW = {
  id: 7,
  status: "unresolved" as const,
  severity: "error" as const,
  alertname: "cluster health",
  labels: { alertname: "cluster health", severity: "error" },
  annotations: { summary: "gateway down" },
  starts_at: "2026-08-12T20:00:00Z",
  ends_at: null,
  fingerprint: "f1",
  generator_url: "",
  source: "health-probe",
  notified_at: null,
  created_at: "2026-08-12T20:00:00Z",
  updated_at: "2026-08-12T20:00:00Z",
};

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("AlertsSection", () => {
  it("renders rows with severity, alert name, summary, status and source", async () => {
    vi.spyOn(api, "getAlerts").mockResolvedValue(
      response({
        alerts: [
          ROW,
          {
            ...ROW,
            id: 8,
            alertname: "machine offline",
            annotations: { summary: "wsl back online" },
            status: "resolved",
            source: "machine-probe",
          },
        ],
        meta: {
          window: "24h",
          total: 2,
          unresolved_count: 1,
        },
      }),
    );
    wrap(<AlertsSection />);
    await waitFor(() => screen.getByTestId("alert-row-7"));
    expect(screen.getByText("cluster health")).toBeTruthy();
    expect(screen.getByText("gateway down")).toBeTruthy();
    expect(screen.getAllByText("error")).toHaveLength(2);
    expect(screen.getByText("unresolved")).toBeTruthy();
    expect(screen.getByText("resolved")).toBeTruthy();
    expect(screen.getByText("health-probe")).toBeTruthy();

    const unresolvedSeverity = within(screen.getByTestId("alert-row-7")).getByText("error");
    expect(unresolvedSeverity.className).toContain("bg-orange-500");

    const resolvedSeverity = within(screen.getByTestId("alert-row-8")).getByText("error");
    expect(resolvedSeverity.className).toContain("bg-muted");
    expect(resolvedSeverity.className).toContain("text-muted-foreground");
    expect(resolvedSeverity.className).not.toContain("bg-orange-500");
  });

  it("shows the empty state when there are no alerts", async () => {
    vi.spyOn(api, "getAlerts").mockResolvedValue(response());
    wrap(<AlertsSection />);
    await waitFor(() => screen.getByText(/No alerts in the last 24h/));
  });

  it("renders a no-horizontal-scroll layout: fixed table, wrapping detail cell, responsive columns (task #1960)", async () => {
    vi.spyOn(api, "getAlerts").mockResolvedValue(
      response({
        alerts: [
          {
            ...ROW,
            alertname: "extremely-long-alert-name-that-must-wrap-instead-of-overflow",
            annotations: {
              summary:
                "an annotation whose full detail must wrap across multiple lines instead of pushing the table wider than the container and forcing a horizontal scroll",
            },
          },
        ],
        meta: { window: "24h", total: 1, unresolved_count: 1 },
      }),
    );
    wrap(<AlertsSection />);
    await waitFor(() => screen.getByTestId("alert-row-7"));

    // Fixed layout: the declared column widths hold and content wraps within
    // them — the table never grows past the container.
    const table = screen.getByRole("table");
    expect(table.className).toContain("table-fixed");

    // The detail (Alert) cell wraps: both the alert name and the summary
    // override the shared TableCell whitespace-nowrap and break long tokens,
    // and the summary is NOT clamped (full multi-line display).
    const row = screen.getByTestId("alert-row-7");
    const nameCell = within(row).getByText(
      "extremely-long-alert-name-that-must-wrap-instead-of-overflow",
    );
    expect(nameCell.className).toContain("whitespace-normal");
    expect(nameCell.className).toContain("break-words");
    const summaryCell = within(row).getByText(/full detail must wrap across multiple lines/);
    expect(summaryCell.className).toContain("whitespace-normal");
    expect(summaryCell.className).toContain("break-words");
    expect(summaryCell.className).not.toContain("line-clamp-2");

    // The secondary columns drop out below md so a phone-width viewport keeps
    // Severity + Alert + State without a horizontal scrollbar. Every td in
    // the row besides severity / alert / state must be hidden lg:table-cell.
    const cells = Array.from(row.querySelectorAll("td"));
    const responsiveHidden = cells.filter((td) => td.className.includes("lg:table-cell"));
    expect(responsiveHidden.length).toBe(3); // Started / Duration / Source
    expect(responsiveHidden.every((td) => td.className.includes("hidden"))).toBe(true);
  });
});
