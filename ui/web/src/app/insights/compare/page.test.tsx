import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clusterArrows,
  compareArrowSpecs,
  type CompareArrowSpec,
} from "@/components/run-timeline/compare-arrows";
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
        messagesMax?: number;
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
import { laneColumnWidth, overlapWindow, sharedCanvasWidth } from "./_view";

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
  options: {
    agentId?: number;
    inbounds?: RunTimelineResponse["inbounds"];
    messages?: RunTimelineResponse["messages"];
  } = {},
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
    ...(options.messages === undefined ? {} : { messages: options.messages }),
  };
}

type TimelineMessage = NonNullable<RunTimelineResponse["messages"]>[number];

/** One raw-context strip message fixture (P4-4 #4023 compare tests). */
function message(idx: number, overrides: Partial<TimelineMessage> = {}): TimelineMessage {
  return {
    key: `c.${idx}`,
    idx,
    ts: "2026-09-17T04:05:00Z",
    kind: "ai",
    source: null,
    chars: 100,
    parts: [{ kind: "think", chars: 100 }],
    ...overrides,
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

  it("views exactly 2–8 valid agents", () => {
    expect(isComparableView(parseCompareAgents("41"))).toBe(false);
    expect(isComparableView(parseCompareAgents("41,42"))).toBe(true);
    expect(isComparableView(parseCompareAgents("41,42,43"))).toBe(true);
    expect(isComparableView(parseCompareAgents("41,42,43,44,45,46,47,48"))).toBe(true);
    expect(isComparableView(parseCompareAgents("41,42,43,44,45,46,47,48,49"))).toBe(false);
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

describe("clusterArrows", () => {
  const spec = (id: number, sourceAgentId: number, targetAgentId: number, ts: string): CompareArrowSpec => ({
    id,
    sourceAgentId,
    targetAgentId,
    ts,
  });

  it("merges same-pair arrows within the pixel tolerance", () => {
    const clusters = clusterArrows(
      [
        { spec: spec(1, 42, 43, "2026-09-17T04:10:00Z"), x: 100 },
        { spec: spec(2, 42, 43, "2026-09-17T04:10:05Z"), x: 105 },
        { spec: spec(3, 42, 43, "2026-09-17T04:12:00Z"), x: 130 },
      ],
      8,
    );

    expect(clusters).toHaveLength(2);
    expect(clusters[0].members.map((member) => member.id)).toEqual([1, 2]);
    expect(clusters[0].x).toBe(100);
    expect(clusters[1].members.map((member) => member.id)).toEqual([3]);
  });

  it("keeps different pairs apart and re-merges the pair's later members", () => {
    const clusters = clusterArrows(
      [
        { spec: spec(1, 42, 43, "2026-09-17T04:10:00Z"), x: 100 },
        { spec: spec(2, 43, 42, "2026-09-17T04:10:01Z"), x: 102 },
        { spec: spec(3, 42, 43, "2026-09-17T04:10:02Z"), x: 103 },
      ],
      8,
    );

    expect(clusters).toHaveLength(2);
    expect(clusters[0].members.map((member) => member.id)).toEqual([1, 3]);
    expect(clusters[1].members.map((member) => member.id)).toEqual([2]);
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

describe("sharedCanvasWidth", () => {
  it("fits the chart's container with no panel open", () => {
    // 26 = the section's p-3 padding (24) + 1px border, each side.
    expect(sharedCanvasWidth(1060, false, true)).toBe(1034);
    expect(sharedCanvasWidth(700, false, true)).toBe(674);
  });

  it("narrows every lane together once a panel claims its column", () => {
    expect(sharedCanvasWidth(1060, true, true)).toBe(1060 - 26 - 332);
    expect(sharedCanvasWidth(400, true, true)).toBe(320);
  });

  it("skips the panel column below lg, where the panel stacks under the chart", () => {
    expect(sharedCanvasWidth(1060, true, false)).toBe(1034);
  });

  it("derives the lane column from the stack width", () => {
    expect(laneColumnWidth(1200)).toBe(1200 - 124);
    expect(laneColumnWidth(80)).toBe(0);
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
      expect(getByRole("alert").textContent).toContain("Choose 2 to 8 agents to compare."),
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
      messagesMax: 200,
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
      messagesMax: 200,
    };
    expect(optionsByAgent.get(42)).toEqual(expected);
    expect(optionsByAgent.get(43)).toEqual(expected);
  });

  it("asks for the configured per-lane strip cap (P4-4)", async () => {
    getSettings.mockResolvedValue({
      settings: [
        { key: "display.run_timeline_compare_messages_max", value: 120, updated_at: NOW.toISOString() },
      ],
    });
    renderPage("42,43");

    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledTimes(2));
    for (const [, options] of getRunTimeline.mock.calls) {
      expect(options?.messagesMax).toBe(120);
    }
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

  it("merges a burst between two agents into one arrow with a count badge", async () => {
    getRunTimeline.mockImplementation((agentId: number) =>
      Promise.resolve(
        agentId === 43
          ? timeline({
              inbounds: [
                { ts: "2026-09-17T04:10:00Z", source: "agent:42", inbound_id: 7 },
                { ts: "2026-09-17T04:10:05Z", source: "agent:42", inbound_id: 8 },
              ],
            })
          : timeline(),
      ),
    );
    renderPage("42,43");

    await waitFor(() => expect(screen.getAllByTestId("compare-arrow")).toHaveLength(1));
    expect(screen.getByTestId("compare-arrow").getAttribute("data-count")).toBe("2");
    expect(screen.getByTestId("compare-arrow-count").textContent).toBe("×2");

    fireEvent.pointerEnter(screen.getByTestId("compare-arrow-hit"));
    await waitFor(() => expect(screen.getByText("#42 → #43 · 04:10 ×2")).toBeTruthy());
  });

  it("renders one strip per lane from the response messages (P4-4)", async () => {
    getRunTimeline.mockImplementation((agentId: number) =>
      Promise.resolve(
        timeline({
          agentId,
          messages:
            agentId === 42 ? [message(0), message(1), message(2)] : [message(0), message(1)],
        }),
      ),
    );
    renderPage("42,43");

    await waitFor(() => expect(screen.getAllByTestId("strip-message-button")).toHaveLength(5));
    expect(screen.getAllByTestId("strip-track")).toHaveLength(2);
  });

  it("renders one shared legend and dims every lane from it (P4-4)", async () => {
    getRunTimeline.mockImplementation((agentId: number) =>
      Promise.resolve(
        timeline({
          agentId,
          messages: [
            message(0),
            message(1, { kind: "exec", parts: [{ kind: "out", chars: 50 }] }),
          ],
        }),
      ),
    );
    renderPage("42,43");
    await waitFor(() => expect(screen.getAllByTestId("strip-message-button")).toHaveLength(4));

    // One page-level legend drives both lanes — per-lane legends stay off.
    expect(screen.getAllByRole("list", { name: "Message categories" })).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "thinking" }));
    const parts = screen.getAllByTestId("strip-part");
    const lit = parts.filter((part) => part.getAttribute("opacity") === "1");
    const dim = parts.filter((part) => part.getAttribute("opacity") === "0.12");
    expect(lit.length).toBeGreaterThan(0);
    expect(lit.every((part) => part.getAttribute("data-strip-color") === "think")).toBe(true);
    // Both lanes carry a dimmed "out" part — the shared selection reached
    // every lane, not just one.
    expect(dim.filter((part) => part.getAttribute("data-strip-color") === "out").length).toBe(2);
    expect(lit.length + dim.length).toBe(parts.length);
  });

  it("focuses the arrows from the inbound highlight without flipping the toggle (P4-4)", async () => {
    getRunTimeline.mockImplementation((agentId: number) =>
      Promise.resolve(
        timeline({
          agentId,
          messages: [message(0)],
          ...(agentId === 43
            ? { inbounds: [{ ts: "2026-09-17T04:10:00Z", source: "agent:42", inbound_id: 7 }] }
            : {}),
        }),
      ),
    );
    const { getByRole } = renderPage("42,43");
    await waitFor(() => expect(screen.getAllByTestId("compare-arrow")).toHaveLength(1));

    const arrowWidth = () => screen.getByTestId("compare-arrow").getAttribute("stroke-width");
    expect(arrowWidth()).toBe("1.5");

    // Toggle off → arrows hidden (their normal off state).
    fireEvent.click(getByRole("button", { name: "Message arrows" }));
    expect(screen.queryAllByTestId("compare-arrow")).toHaveLength(0);

    // Highlighting "agent inbound" pulls them back emphasized — the highlight
    // overrides the toggle without changing it.
    fireEvent.click(getByRole("button", { name: "agent inbound" }));
    await waitFor(() => expect(screen.getAllByTestId("compare-arrow")).toHaveLength(1));
    expect(arrowWidth()).toBe("2.5");
    expect(
      getByRole("button", { name: "Message arrows" }).getAttribute("aria-pressed"),
    ).toBe("false");

    // Releasing the highlight returns to exactly the toggle state (M2).
    fireEvent.click(getByRole("button", { name: "agent inbound" }));
    expect(screen.queryAllByTestId("compare-arrow")).toHaveLength(0);

    fireEvent.click(getByRole("button", { name: "Message arrows" }));
    await waitFor(() => expect(screen.getAllByTestId("compare-arrow")).toHaveLength(1));
    expect(arrowWidth()).toBe("1.5");
  });

  it("prioritizes arrow hover over strip hover in the single readout (P4-4)", async () => {
    getRunTimeline.mockImplementation((agentId: number) =>
      Promise.resolve(
        timeline({
          agentId,
          messages: [message(0)],
          ...(agentId === 43
            ? { inbounds: [{ ts: "2026-09-17T04:10:00Z", source: "agent:42", inbound_id: 7 }] }
            : {}),
        }),
      ),
    );
    renderPage("42,43");
    await waitFor(() => expect(screen.getAllByTestId("compare-arrow")).toHaveLength(1));

    fireEvent.pointerEnter(screen.getAllByTestId("strip-message-button")[0]);
    await waitFor(() =>
      expect(
        screen.getByText(/^#42 · #0 · agent thinking · .* · 100 chars \uFF5C summary: None$/),
      ).toBeTruthy(),
    );

    // Arrow hover wins; leaving the arrow falls back to the strip line.
    fireEvent.pointerEnter(screen.getByTestId("compare-arrow-hit"));
    await waitFor(() => expect(screen.getByText("#42 → #43 · 04:10")).toBeTruthy());
    fireEvent.pointerLeave(screen.getByTestId("compare-arrow-hit"));
    await waitFor(() =>
      expect(
        screen.getByText(/^#42 · #0 · agent thinking · .* · 100 chars \uFF5C summary: None$/),
      ).toBeTruthy(),
    );
  });

  it("explains a degraded strip read once every lane's read failed (P4-4)", async () => {
    getRunTimeline.mockImplementation((agentId: number) =>
      Promise.resolve(
        agentId === 43 ? timeline({ messages: [message(0)] }) : timeline({ messages: null }),
      ),
    );
    const { getByRole } = renderPage("42,43");

    // Partial degradation: one lane still carries strips → legend, no hint.
    await waitFor(() => expect(screen.getAllByTestId("strip-message-button")).toHaveLength(1));
    expect(screen.queryByTestId("strip-read-failed")).toBeNull();
    expect(screen.getAllByRole("list", { name: "Message categories" })).toHaveLength(1);

    // Every lane degrades → the page-level hint explains the absence.
    getRunTimeline.mockImplementation(() => Promise.resolve(timeline({ messages: null })));
    fireEvent.click(getByRole("button", { name: "1h" }));
    await waitFor(() =>
      expect(
        screen.getByText("Message data could not be read: no per-message strip."),
      ).toBeTruthy(),
    );
    expect(screen.queryAllByTestId("strip-message-button")).toHaveLength(0);
    expect(screen.queryByRole("list", { name: "Message categories" })).toBeNull();
  });

  it("renders strips in a bucket window when the response carries messages (P4-4)", async () => {
    getRunTimeline.mockImplementation((agentId: number, options) =>
      Promise.resolve(
        timeline({
          agentId,
          messages:
            options?.level === "bucket"
              ? [message(0), message(1)]
              : [message(0), message(1), message(2)],
        }),
      ),
    );
    const { getByRole } = renderPage("42,43");
    await waitFor(() => expect(screen.getAllByTestId("strip-message-button")).toHaveLength(6));

    // The 6h preset aggregates rows into buckets — the strip read is not gated
    // on the level, so the messages (and their strips) are still there.
    fireEvent.click(getByRole("button", { name: "6h" }));
    await waitFor(() => expect(screen.getAllByTestId("strip-message-button")).toHaveLength(4));
    expect(getRunTimeline.mock.calls.at(-1)?.[1]?.level).toBe("bucket");
    expect(screen.queryByTestId("strip-read-failed")).toBeNull();
    expect(screen.getAllByRole("list", { name: "Message categories" })).toHaveLength(1);
  });

  it("keeps the previous lane response on screen while a window change is in flight (P4-4)", async () => {
    // `useQueries` hands a window change a fresh observer (matched by query
    // hash), so keepPreviousData alone loses the previous response and the
    // lane blanks to "loading" mid-refetch; the parent-side copy keeps the
    // chart on screen (the single view's behavior).
    let calls = 0;
    const pending: ((value: RunTimelineResponse) => void)[] = [];
    getRunTimeline.mockImplementation((agentId: number) => {
      calls += 1;
      if (calls <= 2) {
        return Promise.resolve(timeline({ agentId, messages: [message(0), message(1)] }));
      }
      return new Promise((resolve) => pending.push(resolve));
    });
    const { getByRole } = renderPage("42,43");
    await waitFor(() => expect(screen.getAllByTestId("strip-message-button")).toHaveLength(4));

    fireEvent.click(getByRole("button", { name: "1h" }));
    await waitFor(() => expect(pending.length).toBeGreaterThanOrEqual(1));
    // The refetch is in flight — the lanes still render the previous window.
    expect(screen.queryByText("Loading run timeline…")).toBeNull();
    expect(screen.getAllByTestId("strip-message-button")).toHaveLength(4);

    for (const resolve of pending) {
      resolve(timeline({ messages: [message(0)] }));
    }
    await waitFor(() => expect(screen.getAllByTestId("strip-message-button")).toHaveLength(2));
  });

  it("picks agents from the roster list and rewrites the id value", async () => {
    const { getByLabelText, getByRole } = renderPage();

    const cto = await waitFor(() => getByRole("button", { name: /CTO/ }));
    fireEvent.click(cto);
    expect(getByLabelText("Agents")).toHaveProperty("value", "43");

    fireEvent.click(getByRole("button", { name: /CEO/ }));
    expect(getByLabelText("Agents")).toHaveProperty("value", "43, 42");

    fireEvent.click(getByRole("button", { name: /CTO/ }));
    expect(getByLabelText("Agents")).toHaveProperty("value", "42");
  });

  it("filters the roster list by name or id", async () => {
    renderPage();

    const filter = await waitFor(() => screen.getByLabelText("Filter agents"));
    await waitFor(() => expect(screen.getAllByText(/CEO/).length).toBeGreaterThan(0));

    fireEvent.change(filter, { target: { value: "43" } });
    await waitFor(() => expect(screen.queryByText(/CEO/)).toBeNull());
    expect(screen.getAllByText(/CTO/).length).toBeGreaterThan(0);
  });
});


it("keeps each compare summary folded through the shared summary cap", async () => {
  getRunTimeline.mockImplementation((agentId) => Promise.resolve({
    ...timeline({ agentId }),
    summary: { text: "Long summary text. ".repeat(100), chars: 1900, source: "compact" },
  }));
  renderPage("42,43");
  await waitFor(() => expect(screen.getAllByTestId("raw-summary-toggle")).toHaveLength(2));
  const toggles = screen.getAllByTestId("raw-summary-toggle");
  expect(toggles.map((toggle) => toggle.getAttribute("aria-expanded"))).toEqual(["false", "false"]);
  for (const summary of screen.getAllByTestId("raw-summary")) {
    expect(summary.lastElementChild?.className).toContain("max-h-12");
    expect(summary.lastElementChild?.className).toContain("line-clamp-3");
  }
  fireEvent.click(toggles[0]);
  expect(toggles.map((toggle) => toggle.getAttribute("aria-expanded"))).toEqual(["true", "false"]);
});
