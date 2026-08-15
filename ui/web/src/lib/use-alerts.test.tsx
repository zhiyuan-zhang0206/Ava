// foldAlert — the SSE frame → cache folding logic: upsert by id, count
// deltas for the badge (unread) and the floating bar (unresolved).

import { describe, expect, it } from "vitest";

import { foldAlert } from "@/lib/use-alerts";
import type { Alert, AlertsResponse } from "@/lib/types";

function row(overrides: Partial<Alert> = {}): Alert {
  return {
    id: 1,
    status: "unresolved",
    severity: "error",
    alertname: "r",
    labels: {},
    annotations: {},
    starts_at: "2026-08-12T20:00:00Z",
    ends_at: null,
    fingerprint: "f",
    generator_url: "",
    source: "grafana",
    read_at: null,
    notified_at: null,
    created_at: "2026-08-12T20:00:00Z",
    updated_at: "2026-08-12T20:00:00Z",
    ...overrides,
  };
}

function resp(alerts: Alert[], unread: number, unresolved: number): AlertsResponse {
  return {
    alerts,
    meta: { window: "24h", include_read: true, total: alerts.length, unread_count: unread, unresolved_count: unresolved },
  };
}

describe("foldAlert", () => {
  it("prepends a new firing row and bumps both counts", () => {
    const prev = resp([row({ id: 2, read_at: "2026-08-12T21:00:00Z", status: "resolved" })], 0, 0);
    const next = foldAlert(prev, row({ id: 1 }));
    expect(next.alerts.map((a) => a.id)).toEqual([1, 2]);
    expect(next.meta.unread_count).toBe(1);
    expect(next.meta.unresolved_count).toBe(1);
  });

  it("replaces an existing row and applies transition deltas", () => {
    const prev = resp([row({ id: 1 })], 1, 1);
    const next = foldAlert(prev, row({ id: 1, status: "resolved", ends_at: "2026-08-12T21:00:00Z" }));
    expect(next.alerts).toHaveLength(1);
    expect(next.alerts[0].status).toBe("resolved");
    // still unread, but no longer unresolved
    expect(next.meta.unread_count).toBe(1);
    expect(next.meta.unresolved_count).toBe(0);
  });

  it("decrements the unread count when a row becomes read", () => {
    const prev = resp([row({ id: 1 })], 1, 1);
    const next = foldAlert(prev, row({ id: 1, read_at: "2026-08-12T21:00:00Z" }));
    expect(next.meta.unread_count).toBe(0);
    expect(next.meta.unresolved_count).toBe(1);
  });

  it("seeds a cache from a single frame when nothing is cached yet", () => {
    const next = foldAlert(undefined, row({ id: 5, status: "resolved", read_at: "x" }));
    expect(next.alerts.map((a) => a.id)).toEqual([5]);
    expect(next.meta.unresolved_count).toBe(0);
    expect(next.meta.unread_count).toBe(0);
  });
});
