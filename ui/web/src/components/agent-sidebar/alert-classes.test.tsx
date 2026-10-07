// AlertClassList — the sidebar card's grouped warning/error classes: active rows by count, the
// dismissed ones apart, a row opening to its samples, and dismiss / reopen going through the api.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AlertClassRow, AlertClassesResponse } from "@/lib/contracts/types";

import { AlertClassList } from "./alert-classes";

const apiMock = vi.hoisted(() => ({
  getAlertClasses: vi.fn(),
  getAlertClassSamples: vi.fn(),
  dismissAlertClass: vi.fn(),
  reopenAlertClass: vi.fn(),
}));
vi.mock("@/lib/transport/api", () => ({ api: apiMock }));

function row(overrides: Partial<AlertClassRow>): AlertClassRow {
  return {
    level: "warning",
    event_name: "disk_pressure",
    source: "svc",
    process: "gateway",
    category: "telemetry",
    count: 10_000,
    first_seen: "2026-10-04T01:00:00Z",
    last_seen: "2026-10-04T02:00:00Z",
    dismissal_id: null,
    ...overrides,
  };
}

function response(classes: AlertClassRow[], total = classes.length): AlertClassesResponse {
  return {
    window_hours: 24,
    classes,
    total_classes: total,
    total_events: classes.reduce((sum, c) => sum + c.count, 0),
    as_of: "2026-10-04T02:00:00Z",
  };
}

function renderList() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <AlertClassList windowHours={24} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  Object.values(apiMock).forEach((fn) => fn.mockReset());
  apiMock.getAlertClassSamples.mockResolvedValue([]);
});
afterEach(cleanup);

describe("AlertClassList", () => {
  it("lists each active class with level, source, count and first/last times", async () => {
    apiMock.getAlertClasses.mockResolvedValue(
      response([
        row({}),
        row({ level: "error", event_name: "turn_failed", source: "runner", process: "", count: 41 }),
      ]),
    );
    renderList();

    expect(await screen.findByText("disk_pressure")).toBeTruthy();
    expect(screen.getByText("×10000")).toBeTruthy();
    expect(screen.getByText("WARN")).toBeTruthy();
    expect(screen.getByText("svc · gateway")).toBeTruthy();
    // No process: the source stands alone, no dangling separator.
    expect(screen.getByText("runner")).toBeTruthy();
    expect(screen.getByText("ERROR")).toBeTruthy();
    expect(screen.getByText("×41")).toBeTruthy();
    expect(screen.getAllByText(/^first .* · last .*/)).toHaveLength(2);
    expect(apiMock.getAlertClasses).toHaveBeenCalledWith(24, expect.anything());
  });

  it("keeps dismissed classes out of the active list, behind a collapsed heading", async () => {
    apiMock.getAlertClasses.mockResolvedValue(
      response([row({}), row({ event_name: "old_noise", dismissal_id: 9 })]),
    );
    renderList();

    expect(await screen.findByText("disk_pressure")).toBeTruthy();
    expect(screen.queryByText("old_noise")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Dismissed (1)" }));
    expect(screen.getByText("old_noise")).toBeTruthy();
  });

  it("states the all-clear when only dismissed classes remain", async () => {
    apiMock.getAlertClasses.mockResolvedValue(response([row({ dismissal_id: 3 })]));
    renderList();
    expect(await screen.findByText("No active warning or error classes")).toBeTruthy();
  });

  it("says when the list is cut short of the window's class total", async () => {
    apiMock.getAlertClasses.mockResolvedValue(response([row({})], 350));
    renderList();
    expect(await screen.findByText("Showing the top 1 of 350 classes")).toBeTruthy();
  });

  it("opens a class to its samples and dismisses it by its own identity", async () => {
    const cls = row({ category: "log", process: "agent-host" });
    apiMock.getAlertClasses.mockResolvedValue(response([cls]));
    apiMock.getAlertClassSamples.mockResolvedValue([
      {
        ts: "2026-10-04T02:00:00Z",
        agent_id: 7,
        machine: "macmini",
        trace_id: null,
        message: "disk at 97%",
        attributes: { msg: "disk at 97%" },
      },
    ]);
    apiMock.dismissAlertClass.mockResolvedValue({});
    renderList();

    fireEvent.click(await screen.findByRole("button", { name: /disk_pressure/ }));

    expect(await screen.findByText("disk at 97%")).toBeTruthy();
    expect(screen.getByText(/macmini · agent #7/)).toBeTruthy();
    expect(apiMock.getAlertClassSamples).toHaveBeenCalledWith(
      expect.objectContaining({ level: "warning", event_name: "disk_pressure", process: "agent-host" }),
      24,
      expect.anything(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(apiMock.dismissAlertClass).toHaveBeenCalledOnce());
    expect(apiMock.dismissAlertClass.mock.calls[0][0]).toMatchObject({
      category: "log",
      level: "warning",
      event_name: "disk_pressure",
      source: "svc",
      process: "agent-host",
    });
    // The list is re-read once the dismissal lands.
    await waitFor(() => expect(apiMock.getAlertClasses.mock.calls.length).toBeGreaterThan(1));
  });

  it("reopens a dismissed class by its dismissal id", async () => {
    apiMock.getAlertClasses.mockResolvedValue(response([row({ dismissal_id: 9 })]));
    apiMock.reopenAlertClass.mockResolvedValue({});
    renderList();

    fireEvent.click(await screen.findByRole("button", { name: "Dismissed (1)" }));
    fireEvent.click(screen.getByRole("button", { name: /disk_pressure/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Reopen" }));

    await waitFor(() => expect(apiMock.reopenAlertClass).toHaveBeenCalledWith(9));
  });

  it("shows a failed action's reason on the row", async () => {
    apiMock.getAlertClasses.mockResolvedValue(response([row({})]));
    apiMock.dismissAlertClass.mockRejectedValue(new Error("event class is already dismissed"));
    renderList();

    fireEvent.click(await screen.findByRole("button", { name: /disk_pressure/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Dismiss" }));

    expect(await screen.findByText(/event class is already dismissed/)).toBeTruthy();
  });

  it("offers a retry when the classes cannot be loaded", async () => {
    apiMock.getAlertClasses.mockRejectedValue(new Error("503"));
    renderList();

    // The query's own one retry (1s back-off) runs out before the failure shows.
    const retry = await screen.findByRole("button", { name: "Retry" }, { timeout: 4000 });
    expect(screen.getByText(/Could not load classes: 503/)).toBeTruthy();
    apiMock.getAlertClasses.mockResolvedValue(response([row({})]));
    fireEvent.click(retry);

    expect(await screen.findByText("disk_pressure")).toBeTruthy();
  });
});
