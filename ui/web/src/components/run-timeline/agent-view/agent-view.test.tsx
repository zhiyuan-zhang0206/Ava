import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunTimelineLink, RunTimelineNode, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

const { getRunTimeline, getRunTimelineLinks, getAgentRoster, getContextBreakdown, getRunTimelineContext, getRunTimelineMessages, getSettings, useMediaQuery } =
  vi.hoisted(() => ({
    getRunTimeline: vi.fn<(agentId: number, options?: object) => Promise<RunTimelineResponse>>(),
    getRunTimelineLinks: vi.fn(),
    getAgentRoster: vi.fn(),
    getContextBreakdown: vi.fn(),
    getRunTimelineContext: vi.fn(),
    getRunTimelineMessages: vi.fn(),
    getSettings: vi.fn(),
    useMediaQuery: vi.fn(() => false),
  }));

vi.mock("@/lib/layout/use-media-query", () => ({ useMediaQuery }));
vi.mock("@/lib/transport/api", () => ({
  api: { getRunTimeline, getRunTimelineLinks, getAgentRoster, getContextBreakdown, getRunTimelineContext, getRunTimelineMessages, getSettings },
}));

import AgentViewPage from "@/app/insights/run/[agents]/page";
import { drawn, drawnLinks, itemX, mockCanvas, paintFrame, clickAt } from "../canvas/run-timeline-test-canvas";
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
  inbound_id: null,
  preview: `unit ${i0}`,
  parent,
  context_tokens: 50,
  generation_tokens: null,
  estimated: false,
  session: 0,
  context_total: 100 + i0 * 100,
  request: null,
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
  getRunTimelineLinks.mockReset();
  getRunTimelineLinks.mockResolvedValue({ links: [] });
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

  it("limits the tree to the top levels", async () => {
    render("7,8");
    await waitFor(() => expect(rowIn(8, "run-timeline-row-units")).not.toBeNull());
    fireEvent.change(screen.getByTestId("agent-view-levels"), { target: { value: "1" } });
    expect(rowIn(7, "run-timeline-row-level-2")).not.toBeNull();
    expect(rowIn(7, "run-timeline-row-level-1")).toBeNull();
    expect(rowIn(8, "run-timeline-row-level-1")).not.toBeNull();
    fireEvent.change(screen.getByTestId("agent-view-levels"), { target: { value: "0" } });
    expect(rowIn(7, "run-timeline-row-level-2")).toBeNull();
    expect(rowIn(8, "run-timeline-row-level-1")).toBeNull();
    expect(rowIn(7, "run-timeline-row-units")).not.toBeNull();
  });

  it("draws the Context size row by default and drops it when switched off; there is no Added context row", async () => {
    render("7,8");
    await waitFor(() => expect(rowIn(8, "run-timeline-row-units")).not.toBeNull());
    expect(rowIn(7, "run-timeline-row-context")).not.toBeNull();
    expect(rowIn(8, "run-timeline-row-context")).not.toBeNull();
    expect(screen.queryByTestId("run-timeline-row-added")).toBeNull();
    fireEvent.click(screen.getByTestId("agent-view-context-size"));
    expect(rowIn(7, "run-timeline-row-context")).toBeNull();
    expect(rowIn(8, "run-timeline-row-context")).toBeNull();
    expect(rowIn(7, "run-timeline-row-units")).not.toBeNull();
  });

  it("draws the Messages row by tokens by default, and equal height when asked", async () => {
    render("7");
    await screen.findByTestId("run-timeline-chart");
    expect(screen.getByTestId<HTMLSelectElement>("agent-view-heights").value).toBe("tokens");
    await paintFrame();
    const tall = drawn("units").filter((d) => d.op === "fill").map((d) => d.h);
    fireEvent.change(screen.getByTestId("agent-view-heights"), { target: { value: "equal" } });
    await paintFrame();
    const equal = drawn("units").filter((d) => d.op === "fill").map((d) => d.h);
    expect(new Set(equal).size).toBe(1);
    expect(equal[0]).toBeGreaterThanOrEqual(Math.max(...tall));
  });

  it("walks the arrow keys from one agent's last row into the next agent's first, and back", async () => {
    render("7,8");
    await waitFor(() => expect(rowIn(8, "run-timeline-row-units")).not.toBeNull());
    await paintFrame();
    const live = () => screen.getByTestId("run-timeline-selection-live").textContent;
    // The bar of agent 7's second block, in its last row (Context size).
    const x = itemX(BY_AGENT[7], "input", "utext-1-1", 1000, BASE_78);
    fireEvent.click(within(group(7)).getByTestId("run-timeline-canvas-input"), { clientX: x });
    expect(live()).toContain("Agent #7");
    expect(live()).toContain("unit 1");
    fireEvent.keyDown(window, { key: "ArrowDown" });
    expect(live()).toContain("Agent #8");
    expect(live()).toContain("node l");
    fireEvent.keyDown(window, { key: "ArrowUp" });
    expect(live()).toContain("Agent #7");
    expect(live()).toContain("unit 1");
    // Within an agent, up stays in the agent's own rows: the block in the Messages row, then its tree level.
    fireEvent.keyDown(window, { key: "ArrowUp" });
    fireEvent.keyDown(window, { key: "ArrowUp" });
    expect(live()).toContain("Agent #7");
    expect(live()).toContain("node l");
  });

  it("puts the Messages row and the Context size row on the same blocks, to the pixel", async () => {
    render("7");
    await screen.findByTestId("run-timeline-row-context");
    await paintFrame();
    const units = drawn("units").filter((d) => d.op === "fill").map((d) => [d.x, d.w]);
    const bars = drawn("input").filter((d) => d.op === "fill").map((d) => [d.x, d.w]);
    expect(bars.length).toBeGreaterThan(0);
    expect(bars).toEqual(units);
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

describe("arrows between agents", () => {
  const link = (partial: Partial<RunTimelineLink>): RunTimelineLink => ({
    kind: "send_message",
    ts: at(40),
    sender: 7,
    receiver: 8,
    inbound_id: null,
    fork_from: null,
    preview: null,
    ...partial,
  });
  const LINKS = [
    link({}),
    link({ kind: "spawn", ts: at(35) }),
    link({ kind: "terminate", ts: at(50), sender: 99, receiver: 7 }),
    link({ kind: "fork", ts: at(20), sender: 7, receiver: 98, fork_from: 5 }),
  ];
  const BLUE = "#3b82f6";
  const GREEN = "#22c55e";

  // jsdom lays nothing out: every track is 1000 px wide, 30 px tall, 40 px below the one before.
  const trackRect = (el: Element) => {
    const index = [...document.querySelectorAll("[data-track]")].indexOf(el);
    return { left: 0, top: index * 40, width: 1000, height: 30, right: 1000, bottom: index * 40 + 30, x: 0, y: index * 40, toJSON: () => ({}) };
  };
  const mid = (agent: number | null, row: string) => {
    const scope = agent === null ? screen.getByTestId("agent-view-other-agents") : group(agent);
    const track = within(scope).getByTestId(row).querySelector("[data-track]");
    return (track === null ? 0 : trackRect(track).top) + 15;
  };

  beforeEach(() => {
    getRunTimelineLinks.mockResolvedValue({ links: LINKS });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.hasAttribute("data-track")) return trackRect(this);
      const height = this.dataset.testid === "run-timeline-links-canvas" ? 4000 : 0;
      return { left: 0, top: 0, width: height === 0 ? 0 : 1000, height, right: 1000, bottom: height, x: 0, y: 0, toJSON: () => ({}) };
    });
  });

  const canvasOf = (agent: number, row: string) => within(group(agent)).getByTestId(`run-timeline-canvas-${row}`);
  const ready = async () => {
    getRunTimeline.mockImplementation((agent) => Promise.resolve(BY_AGENT[agent] ?? response(agent, [10, 20], false)));
    render("7,8");
    await screen.findByTestId("agent-view-other-agents");
    await waitFor(() => expect(screen.getAllByTestId("run-timeline-other-event")).toHaveLength(2));
    await paintFrame();
  };
  const strokes = () => drawnLinks().filter((d) => d.op === "stroke");

  it("reads the links of the agents in view over the extent they span together", async () => {
    await ready();
    expect(getRunTimelineLinks).toHaveBeenCalledWith([7, 8], { from: at(0), to: at(120) });
  });

  it("draws one arrow per link in its kind's color, from the sender's Messages row to where the event lands", async () => {
    await ready();
    const byColor = (color: string) => strokes().filter((d) => d.color === color);
    // The message has no inbound block to land on: it ends in agent 8's Messages row at its time.
    const [message] = byColor(BLUE);
    expect(message.x).toBeCloseTo(1000 / 3, 1);
    expect(message.y).toBe(mid(7, "run-timeline-row-units"));
    expect(message.y + message.h).toBe(mid(8, "run-timeline-row-units"));
    // A spawn ends in agent 8's Lifecycle row, which exists although no marker is in its window.
    const [spawn] = byColor(GREEN);
    expect(spawn.y + spawn.h).toBe(mid(8, "run-timeline-row-lifecycle"));
    // Both ends outside the view are drawn to the Other agents row.
    const toOther = strokes().filter((d) => d.y + d.h === mid(null, "run-timeline-row-other"));
    expect(toOther.map((d) => d.color).sort()).toEqual(["#14b8a6"]);
    const fromOther = strokes().filter((d) => d.y === mid(null, "run-timeline-row-other"));
    expect(fromOther.map((d) => d.color)).toEqual(["#ef4444"]);
  });

  it("switches a kind on and off from the legend, with how many there are", async () => {
    await ready();
    const spawn = screen.getByTestId("run-timeline-link-legend-spawn");
    expect(spawn.textContent).toBe("Spawn 1");
    expect(strokes().some((d) => d.color === GREEN)).toBe(true);
    fireEvent.click(spawn);
    await paintFrame();
    expect(spawn.getAttribute("aria-pressed")).toBe("false");
    expect(strokes().some((d) => d.color === GREEN)).toBe(false);
    expect(strokes().some((d) => d.color === BLUE)).toBe(true);
  });

  it("shows the arrow under the pointer in the readout, and a block over it wins", async () => {
    await ready();
    const middleY = (mid(7, "run-timeline-row-units") + mid(8, "run-timeline-row-units")) / 2;
    fireEvent.pointerMove(screen.getByTestId("run-timeline-chart"), { clientX: 1000 / 3, clientY: middleY });
    expect(screen.getByTestId("run-timeline-readout").textContent).toContain("Message · #7 → #8");
    fireEvent.pointerMove(screen.getByTestId("run-timeline-chart"), { clientX: 900, clientY: middleY });
    expect(screen.getByTestId("run-timeline-readout").textContent).not.toContain("→");
    // The pointer over a block of the Messages row: the block is hovered and clicked, not the arrow across it.
    fireEvent.pointerMove(canvasOf(7, "units"), { clientX: itemX(BY_AGENT[7], "units", "utext-0-0", 1000, BASE_78) });
    fireEvent.pointerMove(screen.getByTestId("run-timeline-chart"), { clientX: 1000 / 3, clientY: middleY });
    expect(screen.getByTestId("run-timeline-readout").textContent).not.toContain("→");
  });

  it("selects an arrow by a click on it and shows the event in the details", async () => {
    await ready();
    const middleY = (mid(7, "run-timeline-row-units") + mid(8, "run-timeline-row-units")) / 2;
    fireEvent.pointerMove(screen.getByTestId("run-timeline-chart"), { clientX: 1000 / 3, clientY: middleY });
    fireEvent.click(screen.getByTestId("run-timeline-chart"), { clientX: 1000 / 3, clientY: middleY });
    const detail = await screen.findByTestId("run-timeline-link-detail");
    expect(detail.textContent).toContain("#7");
    expect(detail.textContent).toContain("#8");
    expect(screen.queryByTestId("run-timeline-link-add-agent")).toBeNull();
  });

  it("puts the events of agents not in the view on the Other agents row, and adding one moves its arrow into its own group", async () => {
    await ready();
    const [first, second] = screen.getAllByTestId("run-timeline-other-event");
    expect(first.getAttribute("aria-label")).toBe("Fork from agent 7 to agent 98");
    expect(second.getAttribute("aria-label")).toBe("Terminate from agent 99 to agent 7");
    fireEvent.click(second);
    const detail = await screen.findByTestId("run-timeline-link-detail");
    expect(detail.textContent).toContain("#99 (not in the view)");
    fireEvent.click(screen.getByTestId("run-timeline-link-add-agent"));
    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledWith(99, {}));
  });

  it("walks the Other agents row with the arrow keys, and enters it down from the last agent", async () => {
    await ready();
    const block = canvasOf(8, "units");
    const x = itemX(BY_AGENT[8], "units", "utext-0-0", 1000, BASE_78);
    fireEvent.pointerMove(block, { clientX: x });
    fireEvent.click(block, { clientX: x });
    // Agent 8 is the last: down from its Messages row goes to its Context size row, and from there into
    // the Other agents row, at the event nearest in time (the earlier one on a tie).
    fireEvent.keyDown(window, { key: "ArrowDown" });
    fireEvent.keyDown(window, { key: "ArrowDown" });
    const [fork, terminate] = screen.getAllByTestId("run-timeline-other-event");
    await waitFor(() => expect(fork.getAttribute("aria-pressed")).toBe("true"));
    expect(terminate.getAttribute("aria-pressed")).toBe("false");
    fireEvent.keyDown(window, { key: "ArrowRight" });
    await waitFor(() => expect(terminate.getAttribute("aria-pressed")).toBe("true"));
    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(terminate.getAttribute("aria-pressed")).toBe("true");
    fireEvent.keyDown(window, { key: "ArrowLeft" });
    await waitFor(() => expect(fork.getAttribute("aria-pressed")).toBe("true"));
    // Up leaves the row for the last agent's Messages row.
    fireEvent.keyDown(window, { key: "ArrowUp" });
    await waitFor(() => expect(screen.getByTestId("run-timeline-unit-detail")).toBeTruthy());
  });
});
