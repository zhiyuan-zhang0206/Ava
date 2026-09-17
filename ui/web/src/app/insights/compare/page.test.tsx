import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { compareArrowSpecs } from "@/components/run-timeline/compare-arrows";
import type {
  AgentRoster,
  AgentRow,
  RunTimelineResponse,
  UserSettingListResponse,
} from "@/lib/types";

const { getRunTimeline, getSettings, getAgentRoster, pushSpy } = vi.hoisted(() => ({
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
  getAgentRoster: vi.fn<() => Promise<AgentRoster>>(),
  pushSpy: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: { getRunTimeline, getSettings, getAgentRoster } }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: pushSpy }) }));

import ComparePage, { isComparableView, parseCompareAgents } from "./page";
import { overlapWindow } from "./_view";

const NOW = new Date("2026-09-17T04:30:00.000Z");
const DEFAULT_FROM = "2026-09-17T04:00:00.000Z";
const DEFAULT_TO = "2026-09-17T04:30:00.000Z";

const row: RunTimelineResponse["rows"][number] = {
  turn: 1,
  n_turns: 1,
  start: "2026-09-17T04:05:00Z",
  end: "2026-09-17T04:06:00Z",
  active_s: 60,
  trace_id: "trace-1",
  checkpoint_id: null,
  ok: true,
  llm: {
    calls: 1,
    in_total: 10,
    cache_read: 0,
    out_total: 5,
    reasoning: 0,
    latency_ms: 1000,
    cost_usd: 0.01,
    model: "deepseek-v4-flash",
  },
  execs: [],
  anomalies: [],
  tags: [],
};

function timeline(
  options: { agentId?: number; inbounds?: RunTimelineResponse["inbounds"] } = {},
): RunTimelineResponse {
  return {
    agent_id: options.agentId ?? 0,
    window: { from: DEFAULT_FROM, to: DEFAULT_TO },
    meta: {
      n_turns: 1,
      wall_span_s: 60,
      active_s: 60,
      tokens_in: 10,
      tokens_out: 5,
      cost_usd: 0.01,
      n_exec_failed: 0,
      n_compact: 0,
      n_restart: 0,
      fallback_turns: 0,
      unmatched_turns: 0,
    },
    rows: [row],
    events: [],
    boundaries: {
      initialize_turn: null,
      last_before_compact_turn: null,
      post_window_turns: 0,
      has_activity_after_window: false,
    },
    inbounds: options.inbounds ?? [],
  };
}

function makeAgent(overrides: Partial<AgentRow>): AgentRow {
  return {
    agent_id: 1,
    spawner: "user",
    fork_source_agent_id: null,
    status: "idling",
    pid: 100,
    spawned_at: "2026-05-15T00:00:00Z",
    started_at: "2026-05-15T00:00:00Z",
    last_active_at: "2026-05-15T00:00:00Z",
    last_inbound_at: "2026-05-15T00:00:00Z",
    label: null,
    machine: "test",
    supports_vision: true,
    awaiting_response_count: 0,
    highest_notice_priority: null,
    unread_notice_count: 0,
    heartbeat_paused_until: null,
    liveness_state: "online",
    ...overrides,
  };
}

function renderPage(agents?: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return rtlRender(
    <QueryClientProvider client={queryClient}>
      <ComparePage searchParams={Promise.resolve(agents === undefined ? {} : { agents })} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  getRunTimeline.mockReset();
  getRunTimeline.mockImplementation(() => Promise.resolve(timeline()));
  getSettings.mockReset();
  getSettings.mockResolvedValue({ settings: [] });
  getAgentRoster.mockReset();
  getAgentRoster.mockResolvedValue({
    agents: [
      makeAgent({ agent_id: 42, label: "CEO" }),
      makeAgent({ agent_id: 43, label: "CTO" }),
    ],
    ancestors: [],
  });
  pushSpy.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("parseCompareAgents", () => {
  it("keeps the query order and de-duplicates", () => {
    const selection = parseCompareAgents("43, 41,43");
    expect(selection.ids).toEqual([43, 41]);
    expect(selection.invalid).toBe(false);
  });

  it("flags malformed tokens instead of guessing", () => {
    expect(parseCompareAgents("41, x").invalid).toBe(true);
    expect(parseCompareAgents("-1").invalid).toBe(true);
    expect(parseCompareAgents("2.5").invalid).toBe(true);
    expect(parseCompareAgents("1e3").invalid).toBe(true);
    expect(parseCompareAgents("0x1f").invalid).toBe(true);
  });

  it("ignores empty segments", () => {
    const selection = parseCompareAgents("41,42,");
    expect(selection.ids).toEqual([41, 42]);
    expect(selection.invalid).toBe(false);
  });

  it("views exactly 2–3 valid agents", () => {
    expect(isComparableView(parseCompareAgents("41"))).toBe(false);
    expect(isComparableView(parseCompareAgents("41,42"))).toBe(true);
    expect(isComparableView(parseCompareAgents("41,42,43"))).toBe(true);
    expect(isComparableView(parseCompareAgents("41,42,43,44"))).toBe(false);
    expect(isComparableView(parseCompareAgents("41,42,x"))).toBe(false);
  });
});

describe("compareArrowSpecs", () => {
  it("keeps only deliveries from another visible lane", () => {
    const specs = compareArrowSpecs([
      { agentId: 42, timeline: timeline() },
      {
        agentId: 43,
        timeline: timeline({
          inbounds: [
            { ts: "2026-09-17T04:10:00Z", source: "agent:42", inbound_id: 7 },
            { ts: "2026-09-17T04:11:00Z", source: "user", inbound_id: 8 },
            { ts: "2026-09-17T04:12:00Z", source: "agent:43", inbound_id: 9 },
            { ts: "2026-09-17T04:13:00Z", source: "agent:999", inbound_id: 10 },
          ],
        }),
      },
    ]);

    expect(specs).toEqual([
      { id: 7, sourceAgentId: 42, targetAgentId: 43, ts: "2026-09-17T04:10:00Z" },
    ]);
  });

  it("draws nothing for a degraded or absent inbound list", () => {
    expect(compareArrowSpecs([{ agentId: 42, timeline: timeline({ inbounds: null }) }])).toEqual(
      [],
    );
    expect(compareArrowSpecs([{ agentId: 42, timeline: undefined }])).toEqual([]);
  });

  it("orders arrows by delivery time", () => {
    const specs = compareArrowSpecs([
      {
        agentId: 42,
        timeline: timeline({
          inbounds: [
            { ts: "2026-09-17T04:20:00Z", source: "agent:43", inbound_id: 2 },
            { ts: "2026-09-17T04:10:00Z", source: "agent:43", inbound_id: 1 },
          ],
        }),
      },
      { agentId: 43, timeline: timeline() },
    ]);

    expect(specs.map((spec) => spec.id)).toEqual([1, 2]);
  });
});

describe("overlapWindow", () => {
  it("intersects the row extents of every lane", () => {
    const first = {
      ...timeline(),
      rows: [{ ...row, start: "2026-09-17T04:00:00Z", end: "2026-09-17T04:20:00Z" }],
    };
    const second = {
      ...timeline(),
      rows: [{ ...row, start: "2026-09-17T04:10:00Z", end: "2026-09-17T04:25:00Z" }],
    };

    expect(overlapWindow([first, second])).toEqual({
      from: "2026-09-17T04:10:00.000Z",
      to: "2026-09-17T04:20:00.000Z",
    });
  });

  it("is null when a lane has no rows or the extents do not overlap", () => {
    expect(overlapWindow([timeline(), { ...timeline(), rows: [] }])).toBeNull();
    const late = {
      ...timeline(),
      rows: [{ ...row, start: "2026-09-17T05:00:00Z", end: "2026-09-17T05:10:00Z" }],
    };
    expect(overlapWindow([timeline(), late])).toBeNull();
  });
});

describe("ComparePage", () => {
  it("starts on the selector and pushes a repaired selection", async () => {
    const { getByLabelText, getByRole } = renderPage();

    const button = await waitFor(() => getByRole("button", { name: "Compare agents" }));
    expect(button).toHaveProperty("disabled", true);

    fireEvent.change(getByLabelText("Agents"), { target: { value: "42, 43" } });
    await waitFor(() =>
      expect(getByRole("button", { name: "Compare agents" })).toHaveProperty("disabled", false),
    );
    fireEvent.click(getByRole("button", { name: "Compare agents" }));

    expect(pushSpy).toHaveBeenCalledWith("/insights/compare?agents=42,43");
  });

  it("prefills the selector from a single preselected agent", async () => {
    const { getByLabelText, getByText } = renderPage("42");

    await waitFor(() =>
      expect(getByLabelText("Agents")).toHaveProperty("value", "42"),
    );
    await waitFor(() => expect(getByText("CEO · #42")).toBeTruthy());
  });

  it("flags a malformed selection", async () => {
    const { getByRole } = renderPage("42,x");

    await waitFor(() =>
      expect(getByRole("alert").textContent).toContain("Choose 2 to 3 agents to compare."),
    );
  });

  it("fetches every lane once on one shared window", async () => {
    renderPage("42,43");

    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(2));
    const optionsByAgent = new Map(
      getRunTimeline.mock.calls.map(([agentId, options]) => [agentId, options]),
    );
    expect([...optionsByAgent.keys()].sort()).toEqual([42, 43]);
    expect(optionsByAgent.get(42)).toEqual({
      from: DEFAULT_FROM,
      to: DEFAULT_TO,
      session: "compact",
    });
    expect(optionsByAgent.get(43)).toEqual(optionsByAgent.get(42));
  });

  it("zooms every lane together onto one bucket size", async () => {
    const { getByRole } = renderPage("42,43");
    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(2));

    fireEvent.click(getByRole("button", { name: "24h" }));

    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(4));
    const optionsByAgent = new Map(
      getRunTimeline.mock.calls.slice(2).map(([agentId, options]) => [agentId, options]),
    );
    // The 24h preset centers on the current window but never runs past now,
    // so it lands on the last 24h before the pinned clock.
    const expected = {
      from: "2026-09-16T04:30:00.000Z",
      to: "2026-09-17T04:30:00.000Z",
      level: "bucket",
      bucket: "1800s",
      session: "compact",
    };
    expect(optionsByAgent.get(42)).toEqual(expected);
    expect(optionsByAgent.get(43)).toEqual(expected);
  });

  it("labels lanes and chips with the roster names", async () => {
    renderPage("42,43");

    await waitFor(() => expect(screen.getAllByText("CEO")).toHaveLength(2));
    expect(screen.getAllByText("CTO")).toHaveLength(2);
  });

  it("draws one arrow per lane-to-lane delivery and reads it out on hover", async () => {
    getRunTimeline.mockImplementation((agentId: number) =>
      Promise.resolve(
        agentId === 43
          ? timeline({
              inbounds: [{ ts: "2026-09-17T04:10:00Z", source: "agent:42", inbound_id: 7 }],
            })
          : timeline(),
      ),
    );
    renderPage("42,43");

    await waitFor(() => expect(screen.getAllByTestId("compare-arrow")).toHaveLength(1));

    const hit = screen.getByTestId("compare-arrow-hit");
    fireEvent.pointerEnter(hit);
    await waitFor(() => expect(screen.getByText("#42 → #43 · 04:10")).toBeTruthy());

    fireEvent.pointerLeave(hit);
    await waitFor(() => expect(screen.getByText("Hover a message arrow for details.")).toBeTruthy());
  });

  it("hides the arrows when the toggle is off and restores them", async () => {
    getRunTimeline.mockImplementation((agentId: number) =>
      Promise.resolve(
        agentId === 43
          ? timeline({
              inbounds: [{ ts: "2026-09-17T04:10:00Z", source: "agent:42", inbound_id: 7 }],
            })
          : timeline(),
      ),
    );
    const { getByRole } = renderPage("42,43");
    await waitFor(() => expect(screen.getAllByTestId("compare-arrow")).toHaveLength(1));

    fireEvent.click(getByRole("button", { name: "Message arrows" }));
    expect(screen.queryAllByTestId("compare-arrow")).toHaveLength(0);
    expect(screen.queryByTestId("compare-arrows")).toBeNull();

    fireEvent.click(getByRole("button", { name: "Message arrows" }));
    expect(screen.getAllByTestId("compare-arrow")).toHaveLength(1);
  });
});
