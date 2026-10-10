// CompactingBlock — the ticking "Compacting" block (task #3324). The store
// half is exercised in timeline-store.test.ts; these tests pin the rendered
// states: the running clock, the terminal labels, and the self-hide grace.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LiveCompact } from "@/lib/timeline/timeline-store";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { CompactingBlock } from "./compacting-block";
import { liveClockSubscriberCount } from "./reasoning-clock";

function renderWithQuery(ui: ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  qc.setQueryData(["user-settings"], { "display.show_timestamp_weekday": true });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function setLive(overrides: Partial<LiveCompact> = {}) {
  useTimelineStore.setState({
    liveCompact: {
      compactId: "c1",
      startedAt: "2026-09-14T03:00:00+00:00",
      mode: "request",
      status: null,
      finishedAt: null,
      ...overrides,
    },
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-09-14T03:00:12Z"));
  useTimelineStore.setState({ liveCompact: null });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  useTimelineStore.setState({ liveCompact: null });
});

describe("CompactingBlock", () => {
  it("renders nothing without a live compact", () => {
    renderWithQuery(<CompactingBlock />);
    expect(screen.queryByTestId("compacting-block")).toBeNull();
  });

  it("ticks the running clock from started_at", () => {
    setLive();
    renderWithQuery(<CompactingBlock />);
    expect(screen.getByTestId("compacting-block").dataset.status).toBe("running");
    expect(screen.getByText("Compacting")).toBeTruthy();
    expect(screen.getByTestId("compacting-elapsed").textContent).toBe("12s");
    act(() => {
      vi.advanceTimersByTime(3_000);
    });
    expect(screen.getByTestId("compacting-elapsed").textContent).toBe("15s");
  });

  it("settles on success with the frozen duration", () => {
    setLive({ status: "success", finishedAt: "2026-09-14T03:00:24+00:00" });
    renderWithQuery(<CompactingBlock />);
    expect(screen.getByTestId("compacting-block").dataset.status).toBe("success");
    expect(screen.getByText("Compacted")).toBeTruthy();
    expect(screen.getByTestId("compacting-elapsed").textContent).toBe("24s");
  });

  it("keeps a failure readable, then self-hides after the grace", () => {
    setLive({ status: "failure", finishedAt: "2026-09-14T03:00:05+00:00" });
    renderWithQuery(<CompactingBlock />);
    expect(screen.getByText("Compaction failed")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(40_000);
    });
    expect(screen.queryByTestId("compacting-block")).toBeNull();
  });

  it("self-hides a success when no summary follows (grace expiry)", () => {
    setLive({ status: "success", finishedAt: "2026-09-14T03:00:05+00:00" });
    renderWithQuery(<CompactingBlock />);
    expect(screen.getByTestId("compacting-block")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(3_000);
    });
    expect(screen.queryByTestId("compacting-block")).toBeNull();
  });

  it("stops the live clock once settled; the grace expiry still hides it", () => {
    setLive();
    renderWithQuery(<CompactingBlock />);
    expect(liveClockSubscriberCount()).toBe(1);
    act(() => {
      setLive({ status: "success", finishedAt: "2026-09-14T03:00:10+00:00" });
    });
    expect(liveClockSubscriberCount()).toBe(0);
    act(() => {
      vi.advanceTimersByTime(7_000);
    });
    expect(screen.queryByTestId("compacting-block")).toBeNull();
  });

  it("stops the live clock when a running block self-hides after losing its terminal", () => {
    setLive({ startedAt: "2026-09-14T02:30:30+00:00" });
    renderWithQuery(<CompactingBlock />);
    expect(screen.getByTestId("compacting-block")).toBeTruthy();
    expect(liveClockSubscriberCount()).toBe(1);
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.queryByTestId("compacting-block")).toBeNull();
    expect(liveClockSubscriberCount()).toBe(0);
  });

  it("labels a superseded run", () => {
    setLive({ status: "replaced", finishedAt: "2026-09-14T03:00:04+00:00" });
    renderWithQuery(<CompactingBlock />);
    expect(screen.getByTestId("compacting-block").dataset.status).toBe("replaced");
    expect(screen.getByText("Compaction superseded")).toBeTruthy();
  });

  it("hands over when the store retires the entry (summary landed)", () => {
    setLive({ status: "success", finishedAt: "2026-09-14T03:00:24+00:00" });
    renderWithQuery(<CompactingBlock />);
    expect(screen.getByTestId("compacting-block")).toBeTruthy();
    act(() => {
      useTimelineStore.setState({ liveCompact: null });
    });
    expect(screen.queryByTestId("compacting-block")).toBeNull();
  });
});
