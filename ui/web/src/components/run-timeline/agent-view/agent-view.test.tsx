import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunTimelineNode, RunTimelineMessageBar, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

const { getRunTimeline, getAgentRoster, getContextBreakdown, getRunTimelineContext, getRunTimelineMessages, getSettings, useMediaQuery } =
  vi.hoisted(() => ({
    getRunTimeline: vi.fn<(agentId: number, options?: object) => Promise<RunTimelineResponse>>(),
    getAgentRoster: vi.fn(),
    getContextBreakdown: vi.fn(),
    getRunTimelineContext: vi.fn(),
    getRunTimelineMessages: vi.fn(),
    getSettings: vi.fn(),
    useMediaQuery: vi.fn(() => false),
  }));

vi.mock("@/lib/layout/use-media-query", () => ({ useMediaQuery }));
vi.mock("@/lib/transport/api", () => ({
  api: { getRunTimeline, getAgentRoster, getContextBreakdown, getRunTimelineContext, getRunTimelineMessages, getSettings },
}));

import AgentViewPage from "@/app/insights/run/[agents]/page";
import { itemX, mockCanvas, paintFrame, clickAt } from "../canvas/run-timeline-test-canvas";
import { viewportOf } from "../model/timeline-model";

const T0 = Date.parse("2026-10-04T12:00:00.000Z");
const at = (minutes: number) => new Date(T0 + minutes * 60_000).toISOString();
const usage = { calls: 1, input: 10, cache_read: 0, output: 1, cache_write: 0, cost_usd: 0, cost_calls: 0 };

const node = (id: string, level: number, from: number, to: number, parent: string | null): RunTimelineNode => ({
  id,
  level,
  parent,
  start: at(from),
  end: at(to),
  span_start: 0,
  span_end: 1,
  summary: `node ${id}`,
  usage,
  generation: null,
  context_tokens: null,
  estimated: null,
});

const unit = (i0: number, from: number, to: number, parent: string): RunTimelineUnit => ({
  kind: "text",
  i0,
  i1: i0,
  start: at(from),
  end: at(to),
  source: null,
  preview: `unit ${i0}`,
  parent,
  context_tokens: null,
  generation_tokens: null,
  estimated: null,
});

const bar = (idx: number, minutes: number, total: number, request: boolean): RunTimelineMessageBar => ({
  idx,
  start: at(minutes),
  end: at(minutes + 5),
  session: 0,
  context_tokens: 50,
  estimated: false,
  context_total: total,
  request: request ? { calls: 1, input: total - 50, cache_read: 0, output: 50, cache_write: 0, cost_usd: 0, cost_calls: 0 } : null,
});

const response = (agent: number, window: [number, number], tree: boolean): RunTimelineResponse => ({
  agent_id: agent,
  window: { from: at(window[0]), to: at(window[1]) },
  lifetime: { from: at(window[0]), to: at(window[1]) },
  nodes: tree
    ? [node("t", 2, window[0], window[1], null), node("l", 1, window[0], window[1], "t")]
    : [node("l", 1, window[0], window[1], null)],
  units: [unit(0, window[0] + 5, window[0] + 10, "l"), unit(1, window[0] + 20, window[0] + 25, "l")],
  events: [],
  messages: [bar(0, window[0] + 5, 100, false), bar(1, window[0] + 20, 200, true)],
});

// Agent 7 runs 0-60 min with a two-level tree, agent 8 runs 30-120 min with one level.
const BY_AGENT: Record<number, RunTimelineResponse> = {
  7: response(7, [0, 60], true),
  8: response(8, [30, 120], false),
  9: response(9, [10, 20], false),
};
const BASE_78 = viewportOf({ from: at(0), to: at(120) });

function render(agents = "7") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(
    <QueryClientProvider client={queryClient}>
      <AgentViewPage params={Promise.resolve({ agents })} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockCanvas();
  useMediaQuery.mockReturnValue(false);
  getRunTimeline.mockReset();
  getRunTimeline.mockImplementation((agent) => Promise.resolve(BY_AGENT[agent]));
  getAgentRoster.mockReset();
  getAgentRoster.mockResolvedValue({
    agents: [
      { agent_id: 7, label: "seven", status: "running" },
      { agent_id: 8, label: "eight", status: "running" },
    ],
    ancestors: [],
  });
  getSettings.mockResolvedValue({ settings: [] });
  getRunTimelineContext.mockRejectedValue(new Error("not under test"));
  getContextBreakdown.mockRejectedValue(new Error("not under test"));
  getRunTimelineMessages.mockRejectedValue(new Error("not under test"));
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const group = (agent: number) => screen.getByTestId(`agent-view-agent-${agent}`);
const rowIn = (agent: number, id: string) => within(group(agent)).queryByTestId(id);
const add = (id: number) => {
  fireEvent.change(screen.getByTestId("agent-view-add-input"), { target: { value: String(id) } });
  fireEvent.click(screen.getByTestId("agent-view-add"));
};

describe("agent view", () => {
  it("draws agents named in the URL as groups of rows on one chart", async () => {
    render("7,8");
    await screen.findByTestId("agent-view-agent-8");
    await waitFor(() => expect(within(group(7)).getByTestId("run-timeline-agent").textContent).toBe("Agent #7 · seven"));
    expect(screen.getAllByTestId("run-timeline-chart")).toHaveLength(1);
    expect(getRunTimeline).toHaveBeenCalledWith(7, {});
    expect(getRunTimeline).toHaveBeenCalledWith(8, {});
    // Each agent keeps all of its own rows: agent 7 two tree levels, agent 8 one.
    expect(rowIn(7, "run-timeline-row-level-2")).not.toBeNull();
    expect(rowIn(7, "run-timeline-row-level-1")).not.toBeNull();
    expect(rowIn(8, "run-timeline-row-level-2")).toBeNull();
    expect(rowIn(8, "run-timeline-row-level-1")).not.toBeNull();
    expect(rowIn(8, "run-timeline-row-units")).not.toBeNull();
  });

  it("rejects an id list with a bad entry", async () => {
    render("7,x");
    expect(await screen.findByText("The agent id in this URL is invalid.")).toBeTruthy();
  });

  it("adds an agent by id, keeps it in the URL, and does not take an id twice", async () => {
    render("7");
    await screen.findByTestId("run-timeline-chart");
    expect(screen.queryByTestId("agent-view-remove-7")).toBeNull();
    add(8);
    await screen.findByTestId("agent-view-agent-8");
    await waitFor(() => expect(rowIn(8, "run-timeline-row-units")).not.toBeNull());
    expect(window.location.pathname).toBe("/insights/run/7,8");
    fireEvent.change(screen.getByTestId("agent-view-add-input"), { target: { value: "8" } });
    expect(screen.getByTestId("agent-view-add").hasAttribute("disabled")).toBe(true);
  });

  it("removes an agent, and never the last one", async () => {
    render("7,8");
    await screen.findByTestId("agent-view-agent-8");
    await waitFor(() => expect(rowIn(8, "run-timeline-row-units")).not.toBeNull());
    fireEvent.click(screen.getByTestId("agent-view-remove-7"));
    expect(screen.queryByTestId("agent-view-agent-7")).toBeNull();
    expect(window.location.pathname).toBe("/insights/run/8");
    expect(screen.queryByTestId("agent-view-remove-8")).toBeNull();
  });

  it("limits the tree to the top levels and picks the context bars", async () => {
    render("7,8");
    await waitFor(() => expect(rowIn(8, "run-timeline-row-units")).not.toBeNull());
    // Only the added bars are drawn until the setting says otherwise.
    expect(rowIn(7, "run-timeline-row-context")).toBeNull();
    expect(rowIn(7, "run-timeline-row-added")).not.toBeNull();
    fireEvent.change(screen.getByTestId("agent-view-context"), { target: { value: "both" } });
    expect(rowIn(7, "run-timeline-row-context")).not.toBeNull();
    expect(rowIn(7, "run-timeline-row-added")).not.toBeNull();

    fireEvent.change(screen.getByTestId("agent-view-levels"), { target: { value: "1" } });
    expect(rowIn(7, "run-timeline-row-level-2")).not.toBeNull();
    expect(rowIn(7, "run-timeline-row-level-1")).toBeNull();
    expect(rowIn(8, "run-timeline-row-level-1")).not.toBeNull();
    fireEvent.change(screen.getByTestId("agent-view-levels"), { target: { value: "0" } });
    expect(rowIn(7, "run-timeline-row-level-2")).toBeNull();
    expect(rowIn(8, "run-timeline-row-level-1")).toBeNull();
    expect(rowIn(7, "run-timeline-row-units")).not.toBeNull();

    fireEvent.change(screen.getByTestId("agent-view-context"), { target: { value: "absolute" } });
    expect(rowIn(7, "run-timeline-row-context")).not.toBeNull();
    expect(rowIn(7, "run-timeline-row-added")).toBeNull();
    fireEvent.change(screen.getByTestId("agent-view-context"), { target: { value: "added" } });
    expect(rowIn(7, "run-timeline-row-context")).toBeNull();
    expect(rowIn(7, "run-timeline-row-added")).not.toBeNull();
    fireEvent.change(screen.getByTestId("agent-view-context"), { target: { value: "off" } });
    expect(rowIn(8, "run-timeline-row-context")).toBeNull();
    expect(rowIn(8, "run-timeline-row-added")).toBeNull();
  });

  it("walks the arrow keys from one agent's last row into the next agent's first, and back", async () => {
    render("7,8");
    await waitFor(() => expect(rowIn(8, "run-timeline-row-units")).not.toBeNull());
    await paintFrame();
    const live = () => screen.getByTestId("run-timeline-selection-live").textContent;
    // The request bar of agent 7, in its last row (Added context).
    const x = itemX(BY_AGENT[7], "added", "m1", 1000, BASE_78);
    fireEvent.click(within(group(7)).getByTestId("run-timeline-canvas-added"), { clientX: x });
    expect(live()).toContain("Agent #7");
    expect(live()).toContain("Message 1");
    fireEvent.keyDown(window, { key: "ArrowDown" });
    expect(live()).toContain("Agent #8");
    expect(live()).toContain("node l");
    fireEvent.keyDown(window, { key: "ArrowUp" });
    expect(live()).toContain("Agent #7");
    expect(live()).toContain("Message 1");
    // Within an agent, up stays in the agent's own rows: Context size, then the Messages row.
    fireEvent.keyDown(window, { key: "ArrowUp" });
    fireEvent.keyDown(window, { key: "ArrowUp" });
    expect(live()).toContain("Agent #7");
    expect(live()).not.toContain("Message 1");
  });

  it("does not name an agent in the readout when the view holds one", async () => {
    render("7");
    await screen.findByTestId("run-timeline-chart");
    await paintFrame();
    clickAt("units", itemX(BY_AGENT[7], "units", "utext-0-0"));
    expect(screen.getByTestId("run-timeline-selection-live").textContent).not.toContain("Agent #7");
  });

  it("offers a retry for the one agent that failed", async () => {
    getRunTimeline.mockImplementation((agent) =>
      agent === 8 ? Promise.reject(new Error("boom")) : Promise.resolve(BY_AGENT[agent]),
    );
    render("7,8");
    expect(await screen.findByText("Could not load the timeline of agent 8.")).toBeTruthy();
    expect(rowIn(7, "run-timeline-row-units")).not.toBeNull();
    getRunTimeline.mockImplementation((agent) => Promise.resolve(BY_AGENT[agent]));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(rowIn(8, "run-timeline-row-units")).not.toBeNull());
  });
});
