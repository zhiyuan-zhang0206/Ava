// RunTimelinePage: the /insights/run/[agentId] page — window controls
// (session-route default, from/to inputs, zoom), the run header meta, and the
// dual-panel waterfall (time panel + token panel with independent axes) with
// the session-route line, all from a mocked GET /api/agents/{id}/run-timeline.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render as rtlRender,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import RunTimelinePage from "@/app/insights/run/[agentId]/page";
import type { RunTimelineResponse } from "@/lib/types";

const { getRunTimeline } = vi.hoisted(() => ({
  getRunTimeline: vi.fn<(agentId: number, opts?: { from?: string; to?: string; limit?: number; offset?: number }) => Promise<RunTimelineResponse>>(),
}));
vi.mock("@/lib/api", () => ({
  api: { getRunTimeline },
}));

afterEach(() => {
  cleanup();
  getRunTimeline.mockReset();
});

function render() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const params = Promise.resolve({ agentId: "405" });
  return rtlRender(
    <QueryClientProvider client={qc}>
      <RunTimelinePage params={params} />
    </QueryClientProvider>,
  );
}

function timelineData(overrides: Partial<RunTimelineResponse> = {}): RunTimelineResponse {
  return {
    agent_id: 405,
    window_from: "2026-08-28T16:00:00Z",
    window_to: "2026-08-28T16:02:00Z",
    boundaries: { initialize_at: "2026-08-28T16:00:05Z", compact_at: null },
    meta: {
      n_turns: 2,
      wall_span_s: 120,
      active_s: 8,
      tokens_in: 300,
      tokens_out: 40,
      cost_usd: 0.02,
      n_exec_failed: 0,
      n_compact: 0,
      n_restart: 0,
      truncated: false,
    },
    rows: [
      {
        turn: 1,
        start: "2026-08-28T16:00:05Z",
        end: "2026-08-28T16:00:09Z",
        active_s: 4,
        ok: true,
        trace_id: "a".repeat(32),
        llm: { calls: 1, in_total: 100, cache_read: 99, out_total: 10, reasoning: 2, latency_ms: 500, cost_usd: 0.01, model: "deepseek-v4-flash" },
        execs: [{ tool: "execute_code", dur_s: 1, ok: true }],
        anomalies: [],
        tags: [],
      },
      {
        turn: 2,
        start: "2026-08-28T16:01:00Z",
        end: "2026-08-28T16:01:04Z",
        active_s: 4,
        ok: false,
        trace_id: "b".repeat(32),
        llm: { calls: 1, in_total: 200, cache_read: 198, out_total: 30, reasoning: 5, latency_ms: 700, cost_usd: 0.01, model: "deepseek-v4-flash" },
        execs: [],
        anomalies: ["exec_failed@16:01:02 ValueError"],
        tags: [],
      },
    ],
    ...overrides,
  };
}

describe("RunTimelinePage", () => {
  it("renders the header with the agent id and the session-route default", async () => {
    getRunTimeline.mockResolvedValue(timelineData());
    render();
    expect(await screen.findByText("agent #405")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Run timeline" })).toBeTruthy();
    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledWith(405, { limit: 2000 }));
  });

  it("renders run meta stats from the response", async () => {
    getRunTimeline.mockResolvedValue(timelineData());
    render();
    await screen.findByText("agent #405");
    expect(await screen.findByText("2 turns")).toBeTruthy();
    expect(screen.getByText("$0.020")).toBeTruthy();
    expect(screen.getByText("Σin 300")).toBeTruthy();
  });

  it("renders both panels with their aria labels", async () => {
    getRunTimeline.mockResolvedValue(timelineData());
    render();
    await screen.findByText("agent #405");
    expect(await screen.findByTestId("time-panel")).toBeTruthy();
    expect(screen.getByTestId("token-panel")).toBeTruthy();
    expect(screen.getByTestId("connectors")).toBeTruthy();
  });

  it("expands a turn into its call-level detail", async () => {
    getRunTimeline.mockResolvedValue(timelineData());
    render();
    await screen.findByText("agent #405");
    const tokenPanel = await screen.findByTestId("token-panel");
    const rects = tokenPanel.querySelectorAll("rect");
    expect(rects.length).toBeGreaterThan(0);
    fireEvent.click(rects[0]);
    expect(await screen.findByText(/Turn #1 — calls/)).toBeTruthy();
    expect(screen.getByText("deepseek-v4-flash")).toBeTruthy();
    expect(screen.getByText("execute_code")).toBeTruthy();
  });

  it("renders the session-route line when boundaries are present", async () => {
    getRunTimeline.mockResolvedValue(timelineData());
    render();
    await screen.findByText("agent #405");
    expect(await screen.findByText(/Session route:/)).toBeTruthy();
  });

  it("shows an error state with retry when the fetch fails", async () => {
    getRunTimeline.mockRejectedValue(new Error("boom"));
    render();
    expect(await screen.findByText(/Failed to load/)).toBeTruthy();
    getRunTimeline.mockResolvedValue(timelineData());
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(getRunTimeline.mock.calls.length).toBeGreaterThan(1));
  });
});
