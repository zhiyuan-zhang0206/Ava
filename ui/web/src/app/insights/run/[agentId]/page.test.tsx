import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunTimelineResponse, UserSettingListResponse } from "@/lib/types";

const { getRunTimeline, getSettings } = vi.hoisted(() => ({
  getRunTimeline: vi.fn<
    (
      agentId: number,
      options?: {
        from?: string;
        to?: string;
        level?: "turn" | "bucket";
        bucket?: string;
        session?: "compact" | "current";
      },
    ) => Promise<RunTimelineResponse>
  >(),
  getSettings: vi.fn<() => Promise<UserSettingListResponse>>(),
}));

vi.mock("@/lib/api", () => ({ api: { getRunTimeline, getSettings } }));

import RunTimelinePage from "./page";

const NOW = new Date("2026-09-05T14:26:00.000Z");

// P4-1 trail scope (#4023): a pending stretch gives the chart a focusable
// block, so a double click can push a crumb before an identity switch.
const pendingResponse: RunTimelineResponse = {
  agent_id: 42,
  window: { from: "2026-09-05T14:00:00.000Z", to: "2026-09-05T14:26:00.000Z" },
  meta: {
    n_turns: 1,
    wall_span_s: 1560,
    active_s: 4,
    tokens_in: 120,
    tokens_out: 12,
    cost_usd: 0.02,
    n_exec_failed: 0,
    n_compact: 1,
    n_restart: 0,
    fallback_turns: 0,
    unmatched_turns: 0,
  },
  rows: [
    {
      turn: 1,
      n_turns: 1,
      start: "2026-09-05T14:00:00.000Z",
      end: "2026-09-05T14:00:04.000Z",
      active_s: 4,
      trace_id: null,
      checkpoint_id: null,
      ok: true,
      llm: {
        calls: 1,
        in_total: 120,
        cache_read: 0,
        out_total: 12,
        reasoning: 0,
        latency_ms: 1500,
        cost_usd: 0.02,
        model: "deepseek-flash",
      },
      execs: [],
      anomalies: [],
      tags: [],
    },
  ],
  events: [],
  boundaries: {
    initialize_turn: 1,
    last_before_compact_turn: 1,
    post_window_turns: 0,
    has_activity_after_window: false,
  },
  pending: [{ start: "2026-09-05T14:05:00.000Z", end: "2026-09-05T14:20:00.000Z" }],
};

function render() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return rtlRender(
    <QueryClientProvider client={queryClient}>
      <RunTimelinePage params={Promise.resolve({ agentId: "42" })} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  getRunTimeline.mockReset();
  getRunTimeline.mockReturnValue(new Promise(() => undefined));
  getSettings.mockReset();
  getSettings.mockResolvedValue({ settings: [] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("RunTimelinePage initial window", () => {
  it("does not request the timeline while settings are pending", async () => {
    getSettings.mockReturnValue(new Promise(() => undefined));

    const { getByRole } = render();

    await waitFor(
      () => {
        expect(getSettings).toHaveBeenCalledTimes(1);
        expect(
          getByRole("heading", { name: "Run timeline — agent 42" }),
        ).toBeTruthy();
      },
      { timeout: 500 },
    );
    vi.advanceTimersByTime(60_000);

    expect(getRunTimeline).not.toHaveBeenCalled();
  });

  it("requests the most recent thirty minutes by default", async () => {
    render();

    await waitFor(() =>
      expect(getRunTimeline).toHaveBeenCalledWith(42, {
        from: "2026-09-05T13:56:00.000Z",
        to: "2026-09-05T14:26:00.000Z",
        session: "compact",
      }),
    );
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it("requests the full session after the user resets the window", async () => {
    const { getByRole } = render();

    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(1));

    fireEvent.click(getByRole("button", { name: "Reset window" }));

    await waitFor(() =>
      expect(getRunTimeline).toHaveBeenNthCalledWith(2, 42, { session: "compact" }),
    );
    expect(getRunTimeline).toHaveBeenCalledTimes(2);
    const resetOptions = getRunTimeline.mock.calls[1]?.[1];
    expect(resetOptions).not.toHaveProperty("from");
    expect(resetOptions).not.toHaveProperty("to");
  });

  it("uses the configured positive window duration", async () => {
    getSettings.mockResolvedValue({
      settings: [
        {
          key: "display.run_timeline_window_hours",
          value: 4,
          updated_at: "2026-09-05T14:00:00.000Z",
        },
      ],
    });

    render();

    await waitFor(() =>
      expect(getRunTimeline).toHaveBeenCalledWith(42, {
        from: "2026-09-05T10:26:00.000Z",
        to: "2026-09-05T14:26:00.000Z",
        session: "compact",
      }),
    );
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it.each([0, -1, Number.NaN, "4"])("falls back to thirty minutes for invalid value %s", async (value) => {
    getSettings.mockResolvedValue({
      settings: [
        {
          key: "display.run_timeline_window_hours",
          value,
          updated_at: "2026-09-05T14:00:00.000Z",
        },
      ],
    });

    render();

    await waitFor(() =>
      expect(getRunTimeline).toHaveBeenCalledWith(42, {
        from: "2026-09-05T13:56:00.000Z",
        to: "2026-09-05T14:26:00.000Z",
        session: "compact",
      }),
    );
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });
});

describe("compare entry", () => {
  it("preselects this agent for the compare view", async () => {
    const { getByRole } = render();

    await waitFor(() =>
      expect(getByRole("link", { name: "Compare agents" }).getAttribute("href")).toBe(
        "/insights/compare?agents=42",
      ),
    );
  });
});

describe("trail scope (P4-1)", () => {
  it("clears the focus trail when the session changes", async () => {
    getRunTimeline.mockResolvedValue(pendingResponse);
    const { getByRole, queryByTestId } = render();

    fireEvent.doubleClick(
      await screen.findByRole("button", { name: "Pending layer segment" }),
    );
    expect(await screen.findByTestId("timeline-crumbs")).toBeTruthy();

    fireEvent.click(getByRole("button", { name: "Current session" }));

    await waitFor(() => expect(queryByTestId("timeline-crumbs")).toBeNull());
  });

  it("clears the focus trail when the resolved agentId changes in place", async () => {
    getRunTimeline.mockResolvedValue(pendingResponse);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender, queryByTestId } = rtlRender(
      <QueryClientProvider client={queryClient}>
        <RunTimelinePage params={Promise.resolve({ agentId: "42" })} />
      </QueryClientProvider>,
    );

    fireEvent.doubleClick(
      await screen.findByRole("button", { name: "Pending layer segment" }),
    );
    expect(await screen.findByTestId("timeline-crumbs")).toBeTruthy();

    rerender(
      <QueryClientProvider client={queryClient}>
        <RunTimelinePage params={Promise.resolve({ agentId: "43" })} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(queryByTestId("timeline-crumbs")).toBeNull());
  });
});
