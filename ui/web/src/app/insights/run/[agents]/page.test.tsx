import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ContextBreakdownResponse,
  RunTimelineContext,
  RunTimelineLinks,
  RunTimelineMessages,
  RunTimelineResponse,
  UserSettingListResponse,
} from "@/lib/contracts/types";

const {
  getRunTimeline,
  getRunTimelineMessages,
  getRunTimelineContext,
  getRunTimelineLinks,
  getAgentRoster,
  getSettings,
  getContextBreakdown,
  useMediaQuery,
} =
  vi.hoisted(() => ({
    getRunTimeline:
      vi.fn<
        (agentId: number, options?: { from?: string; to?: string }) => Promise<RunTimelineResponse>
      >(),
    getRunTimelineMessages:
      vi.fn<
        (
          agentId: number,
          range: { start: number; end: number; limit?: number; full?: boolean },
        ) => Promise<RunTimelineMessages>
      >(),
    useMediaQuery: vi.fn(() => false),
    getSettings: vi.fn<() => Promise<UserSettingListResponse>>(),
    getContextBreakdown: vi.fn<(agentId: number) => Promise<ContextBreakdownResponse>>(),
    getRunTimelineContext: vi.fn<(agentId: number, at: number) => Promise<RunTimelineContext>>(),
    getRunTimelineLinks: vi.fn<(agents: readonly number[], window: { from: string; to: string }) => Promise<RunTimelineLinks>>(),
    getAgentRoster: vi.fn(),
  }));

vi.mock("@/lib/layout/use-media-query", () => ({ useMediaQuery }));

vi.mock("@/lib/transport/api", () => ({
  api: { getRunTimeline, getRunTimelineMessages, getRunTimelineContext, getRunTimelineLinks, getAgentRoster, getSettings, getContextBreakdown },
}));

import {
  clickAt,
  drawn,
  itemX,
  leave,
  look,
  mockCanvas,
  paintFrame,
  pointAt,
} from "@/components/run-timeline/canvas/run-timeline-test-canvas";
import RunTimelinePage from "./page";
import Loading from "./loading";

const LIFETIME = { from: "2026-10-04T12:00:00.000000Z", to: "2026-10-04T16:00:00.000000Z" };
const LEAF_A = { from: "2026-10-04T12:00:00.123456Z", to: "2026-10-04T13:00:00.654321Z" };

const usage = { calls: 3, input: 3000, cache_read: 2400, output: 120, cache_write: 0, cost_usd: 0.0123, cost_calls: 3 };

const lifetimeResponse: RunTimelineResponse = {
  agent_id: 42,
  window: LIFETIME,
  lifetime: LIFETIME,
  nodes: [
    {
      id: "1",
      level: 1,
      parent: "3",
      start: LEAF_A.from,
      end: LEAF_A.to,
      span_start: 1,
      span_end: 5,
      summary: "The agent read the repo\nand planned the change.",
      usage,
      generation: { calls: 2, input: 9000, cache_read: 8800, output: 400, seconds: 31.5 },
      context_tokens: 4200,
      estimated: true,
    },
    {
      id: "2",
      level: 1,
      parent: "3",
      start: "2026-10-04T13:00:01.000000Z",
      end: "2026-10-04T16:00:00.000000Z",
      span_start: 6,
      span_end: 9,
      summary: "It implemented and tested it.",
      usage: { calls: 1, input: 100, cache_read: 0, output: 10, cache_write: 0, cost_usd: 0, cost_calls: 0 },
      generation: null,
      context_tokens: null,
      estimated: null,
    },
    {
      id: "3",
      level: 2,
      parent: null,
      start: LEAF_A.from,
      end: "2026-10-04T16:00:00.000000Z",
      span_start: 1,
      span_end: 9,
      summary: "A whole task, start to finish.",
      usage: { calls: 4, input: 3100, cache_read: 2400, output: 130, cache_write: 0, cost_usd: 0, cost_calls: 0 },
      generation: null,
      context_tokens: null,
      estimated: null,
    },
  ],
  units: [
    {
      kind: "inbound",
      i0: 1,
      i1: 1,
      start: "2026-10-04T12:00:00.123456Z",
      end: "2026-10-04T12:00:00.123456Z",
      source: "user",
      inbound_id: null,
      preview: "please fix the bug",
      parent: "1",
      context_tokens: 100,
      generation_tokens: null,
      estimated: true,
      session: 0,
      context_total: 800,
      request: null,
    },
    {
      kind: "output",
      i0: 2,
      i1: 3,
      start: "2026-10-04T12:05:00.000000Z",
      end: "2026-10-04T12:06:00.000000Z",
      source: null,
      inbound_id: null,
      preview: "look at the failing test",
      parent: "1",
      context_tokens: 300,
      generation_tokens: null,
      estimated: false,
      session: 0,
      context_total: 1350,
      request: null,
    },
    // The AIMessage 2 was an LLM request; so was 7, in the second session (after a compaction).
    {
      kind: "text",
      i0: 2,
      i1: 2,
      start: "2026-10-04T12:05:00.000000Z",
      end: "2026-10-04T12:05:00.000000Z",
      source: null,
      inbound_id: null,
      preview: "on it",
      parent: "1",
      context_tokens: 50,
      generation_tokens: null,
      estimated: false,
      session: 0,
      context_total: 1050,
      request: { calls: 1, input: 1000, cache_read: 400, output: 50, cache_write: 0, cost_usd: 0.0021, cost_calls: 1 },
    },
    {
      kind: "thinking",
      i0: 7,
      i1: 7,
      start: "2026-10-04T14:00:00.000000Z",
      end: "2026-10-04T14:00:00.000000Z",
      source: null,
      inbound_id: null,
      preview: "second session",
      parent: "2",
      context_tokens: 20,
      generation_tokens: null,
      estimated: true,
      session: 1,
      context_total: 420,
      request: { calls: 1, input: 400, cache_read: 0, output: 20, cache_write: 0, cost_usd: 0, cost_calls: 0 },
    },
  ],
  events: [{ ts: "2026-10-04T12:00:00.000000Z", kind: "spawn", label: null, source: "user" }],
};

const messagesResponse: RunTimelineMessages = {
  messages: [
    {
      idx: 2,
      ts: "2026-10-04T12:05:00.000000Z",
      source: null,
      parts: [
        { kind: "think", chars: 11, text: "need a plan", text_truncated: false },
        { kind: "text", chars: 5, text: "on it", text_truncated: false },
        { kind: "call", chars: 2, text: "ls", text_truncated: false },
      ],
      context_tokens: 1234,
      estimated: true,
    },
  ],
  next_start: null,
};

const cbdFixture: ContextBreakdownResponse = {
  total_input_tokens: 1000,
  estimated: false,
  exact_fraction: 1,
  max_input_tokens: 1_000_000,
  soft_compact_tokens: 374_000,
  hard_compact_tokens: 512_000,
  sections: [{ name: "(preamble)", tokens: 100, estimated: true }],
  categories: [{ kind: "system_prompt", tokens: 400, estimated: false, exact_fraction: 1 }],
};

function render() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(
    <QueryClientProvider client={queryClient}>
      <RunTimelinePage params={Promise.resolve({ agents: "42" })} />
    </QueryClientProvider>,
  );
}

/** An item of a canvas row, found by what it is; the rows draw no element per item. */
interface Item {
  row: string;
  key: string;
}
const nodeOf = async (id: string): Promise<Item> => {
  await screen.findByTestId("run-timeline-chart");
  const node = lifetimeResponse.nodes.find((candidate) => candidate.id === id);
  if (node === undefined) throw new Error(`no node ${id}`);
  return { row: `level-${node.level}`, key: `n${id}` };
};
const unitOf = async (kind: string): Promise<Item> => {
  await screen.findByTestId("run-timeline-chart");
  const unit = lifetimeResponse.units.find((candidate) => candidate.kind === kind);
  if (unit === undefined) throw new Error(`no ${kind} unit`);
  return { row: "units", key: `u${kind}-${unit.i0}-${unit.i1}` };
};
const xOf = (item: Item) => itemX(lifetimeResponse, item.row, item.key);
const clickItem = (item: Item) => clickAt(item.row, xOf(item));
const hoverItem = (item: Item) => pointAt(item.row, xOf(item));
const leaveItem = (item: Item) => leave(item.row);
/** How an item looks after the next frame: its state ring and whether a highlight faded it. */
const lookOf = async (item: Item) => {
  await paintFrame();
  return look(item.row, xOf(item));
};
const faded = async (item: Item) => (await lookOf(item)).faded;
const ringOf = async (item: Item) => (await lookOf(item)).ring;

beforeEach(() => {
  mockCanvas();
  useMediaQuery.mockReturnValue(false);
  getRunTimeline.mockReset();
  getRunTimelineLinks.mockReset();
  getRunTimelineLinks.mockResolvedValue({ links: [] });
  getRunTimeline.mockResolvedValue(lifetimeResponse);
  getRunTimelineMessages.mockReset();
  getRunTimelineMessages.mockResolvedValue(messagesResponse);
  getSettings.mockReset();
  getSettings.mockResolvedValue({ settings: [] });
  getAgentRoster.mockReset();
  getAgentRoster.mockResolvedValue({
    agents: [{ agent_id: 42, label: "planner", status: "running" }],
    ancestors: [],
  });
  getContextBreakdown.mockReset();
  getContextBreakdown.mockResolvedValue(cbdFixture);
  getRunTimelineContext.mockReset();
  getRunTimelineContext.mockImplementation((_agent, at) => {
    const requests = lifetimeResponse.units.filter((candidate) => candidate.request !== null);
    const request = requests.find((candidate) => candidate.i0 >= at) ?? requests[1];
    return Promise.resolve({
      ...cbdFixture,
      categories: [
        { kind: "system_prompt", tokens: 300, estimated: false, exact_fraction: 1 },
        { kind: "user_input", tokens: 200, estimated: false, exact_fraction: 1 },
        { kind: "reasoning", tokens: 100, estimated: true, exact_fraction: 0 },
      ],
      request: request.i0,
      session: request.session,
      sessions: 2,
      ts: request.start,
    });
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the default window", () => {
  it("asks for the agent's whole lifetime: no from/to", async () => {
    render();
    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledWith(42, {}));
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it("draws every tree level, topmost first, with the message units below", async () => {
    render();
    await screen.findByTestId("run-timeline-chart");
    const rows = screen
      .getAllByTestId(/^run-timeline-row-/)
      .map((row) => row.getAttribute("data-testid"));
    expect(rows).toEqual([
      "run-timeline-row-lifecycle",
      "run-timeline-row-level-2",
      "run-timeline-row-level-1",
      "run-timeline-row-units",
      "run-timeline-row-context",
      "run-timeline-row-other",
    ]);
    // The rows are canvases: one per row, no element per node or block.
    expect(screen.queryAllByTestId("run-timeline-node")).toHaveLength(0);
    await paintFrame();
    expect(screen.getByTestId("run-timeline-canvas-level-1")).toBeTruthy();
    expect(screen.getAllByTestId("run-timeline-event")).toHaveLength(1);
    expect(screen.getByTestId("run-timeline-window").textContent).toContain("3 summary nodes");
  });

  it("has no compare entry and no session switch", async () => {
    render();
    await screen.findByTestId("run-timeline-chart");
    expect(screen.queryByRole("link", { name: "Compare agents" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Compact session" })).toBeNull();
  });

  it("shows the context of the last LLM request in view, not the agent's current context", async () => {
    render();
    await waitFor(() => expect(getRunTimelineContext).toHaveBeenCalledWith(42, 7));
    expect(getContextBreakdown).not.toHaveBeenCalled();
    expect((await screen.findByTestId("context-breakdown-heading")).textContent).toContain("request · session 2 of 2");
  });
});

describe("ancestors", () => {
  it("lights a node and its ancestors, and steps the others back", async () => {
    render();
    clickItem(await nodeOf("1"));
    expect(await ringOf(await nodeOf("1"))).toBe("primary");
    expect(await ringOf(await nodeOf("3"))).toBe("linked");
    expect(await ringOf(await nodeOf("2"))).toBe("none");
  });

  it("lights a message block's covering leaf and every ancestor above it", async () => {
    render();
    clickItem(await unitOf("text"));
    expect(await ringOf(await unitOf("text"))).toBe("primary");
    expect(await ringOf(await nodeOf("1"))).toBe("linked");
    expect(await ringOf(await nodeOf("3"))).toBe("linked");
    expect(await ringOf(await nodeOf("2"))).toBe("none");
  });

});

describe("selecting", () => {
  it("shows a node's summary, span and both costs in the side panel", async () => {
    render();
    clickItem(await nodeOf("1"));

    const detail = await screen.findByTestId("run-timeline-node-detail");
    expect(within(detail).getByTestId("run-timeline-summary").textContent).toBe(
      "The agent read the repo\nand planned the change.",
    );
    expect(detail.textContent).toContain("5 messages");
    // One Details section holds the span's facts and the agent's own usage; the summary's generation cost is not shown.
    const facts = within(detail).getByTestId("run-timeline-details");
    expect(facts.textContent).toContain("Time");
    expect(facts.textContent).toContain("Calls");
    expect(detail.textContent).not.toContain("Agent cost over this span");
    expect(detail.textContent).not.toContain("Cost of generating this summary");
    expect(detail.textContent).not.toContain("of input from cache");
    // the agent's own cost: 3 calls, 3.0k input, 2.4k cache read
    expect(within(detail).getAllByText("3.0k").length).toBeGreaterThan(0);
    expect(within(detail).getAllByText("2.4k").length).toBeGreaterThan(0);
  });

  it("names the agent above its rows with no present-state facts", async () => {
    render();
    expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Agent view");
    await waitFor(() => expect(screen.getByTestId("run-timeline-agent").textContent).toBe("Agent #42 · planner"));
    expect(screen.queryByTestId("run-timeline-status")).toBeNull();
    expect(screen.queryByTestId("run-timeline-model")).toBeNull();
  });

  it("renders a message's Markdown and shows the tokens it occupies, marked when estimated", async () => {
    getRunTimelineMessages.mockResolvedValue({
      messages: [
        {
          idx: 2,
          ts: "2026-10-04T12:05:00.000000Z",
          source: null,
          parts: [{ kind: "text", chars: 9, text: "**bold** x", text_truncated: false }],
          context_tokens: 1234,
          estimated: true,
        },
      ],
      next_start: null,
    });
    render();
    clickItem(await nodeOf("1"));
    const list = await screen.findByTestId("run-timeline-messages");
    expect((await within(list).findByText("bold")).tagName).toBe("STRONG");
    expect(within(list).getByTestId("run-timeline-message-tokens").textContent).toBe("~1.2k tokens");
    const detail = screen.getByTestId("run-timeline-node-detail");
    expect(within(detail).getByText("Details")).toBeTruthy();
  });

  it("reads a unit's own raw parts: a text unit shows the text, not the thinking or the call", async () => {
    render();
    clickItem(await unitOf("text"));

    const detail = await screen.findByTestId("run-timeline-unit-detail");
    expect(within(detail).queryByRole("checkbox")).toBeNull();
    await waitFor(() =>
      expect(getRunTimelineMessages).toHaveBeenCalledWith(42, {
        start: 2,
        end: 2,
        limit: 50,
        full: true,
      }),
    );
    expect(await within(detail).findByText("on it")).toBeTruthy();
    // The unit's tokens are its part's share, not the whole message's.
    expect(within(detail).getAllByTestId("run-timeline-message-tokens")).toHaveLength(1);
    expect(within(detail).queryByText("need a plan")).toBeNull();
    expect(within(detail).queryByText("ls")).toBeNull();
  });
});

function wheel(target: Element, init: WheelEventInit, count = 1) {
  act(() => {
   for (let i = 0; i < count; i++) {
    // jsdom's WheelEvent drops the pointer coordinates; a MouseEvent carries them.
    const event = new MouseEvent("wheel", { bubbles: true, cancelable: true, clientX: init.clientX });
    Object.defineProperties(event, {
      deltaX: { value: init.deltaX ?? 0 },
      deltaY: { value: init.deltaY ?? 0 },
    });
    target.dispatchEvent(event);
   }
  });
}

const summaryText = () => screen.getByTestId("run-timeline-window").textContent;

describe("no drill-in", () => {
  it("a double-click neither zooms nor drills, there is no Drill button, no breadcrumb and no sessions panel", async () => {
    render();
    const whole = (await screen.findByTestId("run-timeline-window")).textContent;
    const target = await nodeOf("1");
    clickItem(target);
    fireEvent.doubleClick(screen.getByTestId(`run-timeline-canvas-${target.row}`));
    expect(await screen.findByTestId("run-timeline-node-detail")).toBeTruthy();
    expect(summaryText()).toBe(whole);
    expect(screen.queryByRole("button", { name: "Drill in" })).toBeNull();
    expect(screen.queryByTestId("run-timeline-crumbs")).toBeNull();
    expect(screen.queryByTestId("run-timeline-sessions-toggle")).toBeNull();
  });
});

describe("zoom and pan", () => {
  beforeEach(() => {
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const wide = this.hasAttribute("data-track");
      return { left: 100, top: 0, width: wide ? 1000 : 0, height: 0, right: 1100, bottom: 0, x: 100, y: 0, toJSON: () => ({}) };
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("the wheel zooms around the cursor and the loaded data is not read again", async () => {
    render();
    const chart = await screen.findByTestId("run-timeline-chart");
    const whole = summaryText();
    wheel(chart, { deltaY: -400, clientX: 100 });
    const zoomed = summaryText();
    expect(zoomed).not.toBe(whole);
    // cursor at the left edge: the start stays, so the view starts where the lifetime did
    expect(zoomed.split(" – ")[0]).toBe(whole.split(" – ")[0]);
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it("wheel events that arrive before React renders compose instead of repeating the old view", async () => {
    render();
    const chart = await screen.findByTestId("run-timeline-chart");
    wheel(chart, { deltaY: -300, clientX: 600 });
    wheel(chart, { deltaY: -300, clientX: 600 });
    const sequential = summaryText();
    fireEvent.click(screen.getByRole("button", { name: "Reset zoom" }));
    wheel(chart, { deltaY: -300, clientX: 600 }, 2);
    expect(summaryText()).toBe(sequential);
  });

  it("a horizontal scroll pans a zoomed view", async () => {
    render();
    const chart = await screen.findByTestId("run-timeline-chart");
    wheel(chart, { deltaY: -600, clientX: 100 });
    const zoomed = summaryText();
    wheel(chart, { deltaX: 300, deltaY: 0, clientX: 600 });
    expect(summaryText()).not.toBe(zoomed);
  });

  it("a drag pans and its release does not select the block under the pointer", async () => {
    render();
    const chart = await screen.findByTestId("run-timeline-chart");
    wheel(chart, { deltaY: -600, clientX: 100 });
    const zoomed = summaryText();
    // The block at the left edge of the view (the rects are mocked at left 100).
    const unit = screen.getByTestId("run-timeline-canvas-units");
    fireEvent.pointerDown(unit, { clientX: 400, button: 0, pointerId: 1 });
    fireEvent.pointerMove(unit, { clientX: 200, pointerId: 1 });
    fireEvent.pointerUp(unit, { clientX: 200, pointerId: 1 });
    fireEvent.click(unit, { clientX: 100 });
    expect(summaryText()).not.toBe(zoomed);
    expect(screen.queryByTestId("run-timeline-unit-detail")).toBeNull();
  });

  it("a press that barely moves is still a click", async () => {
    render();
    const human = await unitOf("inbound");
    const unit = screen.getByTestId("run-timeline-canvas-units");
    fireEvent.pointerDown(unit, { clientX: 400, button: 0, pointerId: 1 });
    fireEvent.pointerUp(unit, { clientX: 401, pointerId: 1 });
    // The canvas rects are mocked at left 100.
    clickAt(human.row, xOf(human) + 100);
    expect(await screen.findByTestId("run-timeline-unit-detail")).toBeTruthy();
  });

  it("the buttons zoom and the fit button restores the whole lifetime", async () => {
    render();
    await screen.findByTestId("run-timeline-chart");
    const whole = summaryText();
    expect(screen.getByRole("button", { name: "Reset zoom" }).hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(summaryText()).not.toBe(whole);
    fireEvent.click(screen.getByRole("button", { name: "Reset zoom" }));
    expect(summaryText()).toBe(whole);
  });
});

describe("failure and loading", () => {
  it("offers a retry when the read fails", async () => {
    getRunTimeline.mockRejectedValueOnce(new Error("boom"));
    render();
    expect(await screen.findByText("Could not load the timeline of agent 42.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByTestId("run-timeline-chart");
  });

  it("the route loading shell renders", () => {
    const { container } = rtlRender(<Loading />);
    expect(container.querySelector("main")).not.toBeNull();
  });

  it("clicking a thinking block reads only the thinking, a tool call block only the call", async () => {
    const response: RunTimelineResponse = {
      ...lifetimeResponse,
      units: [
        {
          kind: "thinking",
          i0: 2,
          i1: 2,
          start: "2026-10-04T12:04:00.000000Z",
          end: "2026-10-04T12:05:00.000000Z",
          source: null,
          inbound_id: null,
          preview: "need a plan",
          parent: "1",
          context_tokens: null,
          generation_tokens: null,
          estimated: null,
          session: 0,
          context_total: null,
          request: null,
        },
        {
          kind: "call",
          i0: 2,
          i1: 2,
          start: "2026-10-04T12:05:00.000000Z",
          end: "2026-10-04T12:05:00.000000Z",
          source: null,
          inbound_id: null,
          preview: "ls",
          parent: "1",
          context_tokens: null,
          generation_tokens: null,
          estimated: null,
          session: 0,
          context_total: null,
          request: null,
        },
      ],
    };
    getRunTimeline.mockResolvedValue(response);
    render();
    await screen.findByTestId("run-timeline-canvas-units");
    clickAt("units", itemX(response, "units", "uthinking-2-2"));
    let detail = await screen.findByTestId("run-timeline-unit-detail");
    expect(await within(detail).findByText("need a plan")).toBeTruthy();
    expect(within(detail).queryByText("ls")).toBeNull();
    expect(within(detail).queryByText("on it")).toBeNull();

    clickAt("units", itemX(response, "units", "ucall-2-2"));
    detail = await screen.findByTestId("run-timeline-unit-detail");
    expect(await within(detail).findByText((_, el) => el?.tagName === "PRE" && el.textContent === "ls")).toBeTruthy();
    expect(within(detail).queryByText("need a plan")).toBeNull();
  });

  it("draws every block on one lane and lists the block colors in a legend", async () => {
    render();
    await screen.findByTestId("run-timeline-chart");
    await paintFrame();
    expect(screen.getAllByTestId(/^run-timeline-canvas-/).map((el) => el.getAttribute("data-testid"))).toEqual([
      "run-timeline-canvas-level-2",
      "run-timeline-canvas-level-1",
      "run-timeline-canvas-units",
      "run-timeline-canvas-input",
    ]);
    const legend = screen.getByTestId("run-timeline-legend");
    expect(within(legend).getAllByRole("listitem")).toHaveLength(7);
  });
});

describe("legend highlight", () => {
  it("lights one class, fades every other block and every summary block, and clears on a second click", async () => {
    render();
    const human = await unitOf("inbound");
    const text = await unitOf("text");
    const legendHuman = screen.getByTestId("run-timeline-legend-human");
    expect(legendHuman.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(legendHuman);
    expect(legendHuman.getAttribute("aria-pressed")).toBe("true");
    expect(await faded(human)).toBe(false);
    expect(await faded(text)).toBe(true);
    expect(await faded(await nodeOf("1"))).toBe(true);
    expect(await faded(await nodeOf("3"))).toBe(true);

    fireEvent.click(legendHuman);
    expect(legendHuman.getAttribute("aria-pressed")).toBe("false");
    expect(await faded(text)).toBe(false);
    expect(await faded(await nodeOf("1"))).toBe(false);
  });

  it("keeps the highlight through a zoom", async () => {
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const wide = this.hasAttribute("data-track");
      return { left: 100, top: 0, width: wide ? 1000 : 0, height: 0, right: 1100, bottom: 0, x: 100, y: 0, toJSON: () => ({}) };
    });
    render();
    const chart = await screen.findByTestId("run-timeline-chart");
    fireEvent.click(screen.getByTestId("run-timeline-legend-human"));
    wheel(chart, { deltaY: -400, clientX: 100 });
    await paintFrame();
    // Zoomed at the left edge, the view starts where it did: the first block is still at x 0.
    const [inbound, text] = [await unitOf("inbound"), await unitOf("text")];
    const probe = screen.getByTestId("run-timeline-canvas-units");
    expect(probe).toBeTruthy();
    expect(inbound.row).toBe(text.row);
    expect(screen.getByTestId("run-timeline-legend-human").getAttribute("aria-pressed")).toBe("true");
  });

  it("links to the context breakdown rows: a row lights its class, and the legend lights its row", async () => {
    render();
    const row = await screen.findByTestId("context-breakdown-category-user_input");
    expect(row.getAttribute("aria-pressed")).toBe("false");
    // A category with no blocks on the timeline is not a button.
    expect(screen.queryByTestId("context-breakdown-category-system_prompt")).toBeNull();
    fireEvent.click(row);
    expect(screen.getByTestId("run-timeline-legend-human").getAttribute("aria-pressed")).toBe("true");
    expect(await faded(await unitOf("text"))).toBe(true);
    expect(row.getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(screen.getByTestId("run-timeline-legend-human"));
    expect(row.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(screen.getByTestId("run-timeline-legend-thinking"));
    expect(screen.getByTestId("context-breakdown-category-reasoning").getAttribute("aria-pressed")).toBe("true");
  });

  it("splits inbound blocks by source: highlighting one agent's inbound fades the other's", async () => {
    const sourceResponse: RunTimelineResponse = {
      ...lifetimeResponse,
      units: [
        { ...lifetimeResponse.units[0], kind: "inbound", i0: 1, i1: 1, source: "agent:12", preview: "from twelve" },
        { ...lifetimeResponse.units[0], kind: "inbound", i0: 3, i1: 3, source: "agent:9", start: "2026-10-04T12:10:00.000000Z", end: "2026-10-04T12:10:00.000000Z", preview: "from nine" },
      ],
    };
    getRunTimeline.mockResolvedValue(sourceResponse);
    render();
    fireEvent.click(await screen.findByTestId("run-timeline-legend-agent"));
    const select = await screen.findByTestId("run-timeline-source-select");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "All sources",
      "Inbound from agent 12",
      "Inbound from agent 9",
    ]);
    fireEvent.change(select, { target: { value: "agent:12" } });
    const twelve: Item = { row: "units", key: "uinbound-1-1" };
    const nine: Item = { row: "units", key: "uinbound-3-3" };
    const at = (item: Item) => itemX(sourceResponse, item.row, item.key);
    const fadedAt = async (item: Item) => {
      await paintFrame();
      return look(item.row, at(item)).faded;
    };
    expect(await fadedAt(twelve)).toBe(false);
    expect(await fadedAt(nine)).toBe(true);
    fireEvent.change(select, { target: { value: "" } });
    expect(await fadedAt(nine)).toBe(false);
  });
});

describe("hover", () => {
  const readout = () => screen.getByTestId("run-timeline-readout").textContent;
  // A hover is lit softly: the 1 px ring (the hovered item itself is the stronger of the two).
  const ringAfter = async (item: Item) => ringOf(item);

  it("reads a block's kind, message span, read time, source and preview, and restores on leave", async () => {
    render();
    const idle = (await screen.findByTestId("run-timeline-readout")).textContent;
    const human = await unitOf("inbound");
    hoverItem(human);
    expect(readout()).toContain("Human message · 1 message · read");
    expect(readout()).toContain("user · please fix the bug");
    leaveItem(human);
    expect(readout()).toBe(idle);
  });

  it("reads a node's level, span, summary first line and the agent's own usage", async () => {
    render();
    hoverItem(await nodeOf("1"));
    const text = readout();
    expect(text).toContain("Level 1");
    expect(text).toContain("5 messages");
    expect(text).toContain("The agent read the repo");
    expect(text).not.toContain("and planned");
    expect(text).toContain("3 calls · 3.0k in · 120 out");
    expect(text).toContain("~4.2k tokens");
  });

  it("lights a hovered block's ancestor chain softly", async () => {
    render();
    hoverItem(await unitOf("text"));
    expect(await ringAfter(await nodeOf("1"))).toBe("hover");
    expect(await ringAfter(await nodeOf("3"))).toBe("hover");
    expect(await ringAfter(await nodeOf("2"))).toBe("none");
    leaveItem(await unitOf("text"));
    expect(await ringAfter(await nodeOf("1"))).toBe("none");
  });

  it("lights a hovered node's covered blocks and its ancestors", async () => {
    render();
    hoverItem(await nodeOf("1"));
    expect(await ringAfter(await nodeOf("1"))).toBe("hover");
    expect(await ringAfter(await nodeOf("3"))).toBe("hover");
    expect(await ringAfter(await unitOf("inbound"))).toBe("hover");
    expect(await ringAfter(await unitOf("text"))).toBe("hover");
    // node 2 is a sibling: no block of it is covered
    expect(await ringAfter(await nodeOf("2"))).toBe("none");
  });

  it("gives the selection priority over the hover", async () => {
    render();
    clickItem(await nodeOf("2"));
    hoverItem(await unitOf("text"));
    expect(await ringOf(await nodeOf("2"))).toBe("primary");
    expect(await ringOf(await nodeOf("3"))).toBe("linked");
    expect(await ringOf(await nodeOf("1"))).toBe("hover");
  });
});

describe("side panel links", () => {
  it("lists a node's ancestors and children as chips that jump to them", async () => {
    render();
    clickItem(await nodeOf("1"));
    let detail = await screen.findByTestId("run-timeline-node-detail");
    const up = within(detail).getAllByTestId("run-timeline-chip");
    expect(up.map((chip) => chip.textContent)).toEqual(["Level 2 · A whole task, start to finish."]);
    fireEvent.click(up[0]);

    detail = await screen.findByTestId("run-timeline-node-detail");
    expect(detail.textContent).toContain("Level 2 summary");
    expect(await ringOf(await nodeOf("3"))).toBe("primary");
    const down = within(detail).getAllByTestId("run-timeline-chip");
    expect(down.map((chip) => chip.getAttribute("data-node-id"))).toEqual(["1", "2"]);
    fireEvent.click(down[1]);
    expect(await ringOf(await nodeOf("2"))).toBe("primary");
  });

  it("shows the summary block a message block belongs to, and jumps to it", async () => {
    render();
    clickItem(await unitOf("text"));
    const detail = await screen.findByTestId("run-timeline-unit-detail");
    fireEvent.click(within(detail).getByTestId("run-timeline-chip"));
    expect(await ringOf(await nodeOf("1"))).toBe("primary");
    expect(await screen.findByTestId("run-timeline-node-detail")).toBeTruthy();
  });

  it("says so when no summary block covers a message block", async () => {
    getRunTimeline.mockResolvedValue({
      ...lifetimeResponse,
      units: [{ ...lifetimeResponse.units[0], parent: null }],
    });
    render();
    clickItem(await unitOf("inbound"));
    expect(await screen.findByTestId("run-timeline-uncovered")).toBeTruthy();
  });
});

describe("context breakdown follows the point", () => {
  it("follows a selected block, then a selected node, and is titled with its session and time", async () => {
    render();
    await waitFor(() => expect(getRunTimelineContext).toHaveBeenLastCalledWith(42, 7));
    clickItem(await unitOf("text"));
    await waitFor(() => expect(getRunTimelineContext).toHaveBeenLastCalledWith(42, 2));
    await waitFor(() =>
      expect(screen.getByTestId("context-breakdown-heading").textContent).toContain("request · session 1 of 2"),
    );
    clickItem(await nodeOf("2"));
    await waitFor(() => expect(getRunTimelineContext).toHaveBeenLastCalledWith(42, 6));
    await waitFor(() =>
      expect(screen.getByTestId("context-breakdown-heading").textContent).toContain("request · session 2 of 2"),
    );
  });

  it("without a selection follows the last request inside the viewport", async () => {
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const wide = this.hasAttribute("data-track");
      return { left: 100, top: 0, width: wide ? 1000 : 0, height: 0, right: 1100, bottom: 0, x: 100, y: 0, toJSON: () => ({}) };
    });
    render();
    const chart = await screen.findByTestId("run-timeline-chart");
    await waitFor(() => expect(getRunTimelineContext).toHaveBeenLastCalledWith(42, 7));
    // Zooming at the left edge leaves only the first request (12:05) in view.
    wheel(chart, { deltaY: -600, clientX: 100 });
    await waitFor(() => expect(getRunTimelineContext).toHaveBeenLastCalledWith(42, 2));
    vi.restoreAllMocks();
  });

  it("says so when the agent has made no request", async () => {
    getRunTimeline.mockResolvedValue({ ...lifetimeResponse, units: lifetimeResponse.units.map((u) => ({ ...u, context_tokens: null, context_total: null, request: null })) });
    render();
    expect((await screen.findByTestId("context-breakdown-empty")).textContent).toContain("no LLM request");
    expect(getRunTimelineContext).not.toHaveBeenCalled();
  });
});

describe("context size row", () => {
  it("draws one bar per block, as tall as the context through it, and reads its value on hover", async () => {
    render();
    await screen.findByTestId("run-timeline-chart");
    await paintFrame();
    const key = (k: string) => ({ row: "input", key: k });
    const barAt = (k: string) => {
      const x = xOf(key(k));
      return drawn("input").filter((d) => d.op === "fill" && d.x <= x && x <= d.x + d.w).at(-1);
    };
    const first = barAt("utext-2-2");
    const second = barAt("uthinking-7-7");
    expect(second?.h ?? 0).toBeGreaterThan(0);
    expect((second?.h ?? 0) / (first?.h ?? 1)).toBeCloseTo(420 / 1050);
    // Session 0 is blue, session 1 amber.
    expect(first?.color).toContain("#3b82f6");
    expect(second?.color).toContain("#f59e0b");
    pointAt("input", xOf(key("uthinking-7-7")));
    expect(screen.getByTestId("run-timeline-readout").textContent).toContain("~20 tokens · context through it 420");
  });

  it("has exactly the blocks of the Messages row, to the pixel", async () => {
    render();
    await screen.findByTestId("run-timeline-chart");
    await paintFrame();
    const fillsOf = (row: string) => drawn(row).filter((d) => d.op === "fill").map((d) => [d.x, d.w]);
    expect(fillsOf("input")).toEqual(fillsOf("units"));
    expect(fillsOf("input")).toHaveLength(lifetimeResponse.units.length);
  });

  it("shows the LLM request of an AIMessage in the details of each of its blocks, not on the timeline", async () => {
    render();
    await screen.findByTestId("run-timeline-chart");
    await paintFrame();
    clickAt("input", xOf({ row: "input", key: "utext-2-2" }));
    const request = await screen.findByTestId("run-timeline-request");
    expect(request.textContent).toContain("1.0k");
    expect(request.textContent).toContain("$0.0021");
    // A block that was no request has no such section.
    clickAt("input", xOf({ row: "input", key: "uinbound-1-1" }));
    await waitFor(() => expect(screen.queryByTestId("run-timeline-request")).toBeNull());
  });

  it("switches off, and has no Added context row", async () => {
    render();
    await screen.findByTestId("run-timeline-chart");
    expect(screen.queryByTestId("run-timeline-row-added")).toBeNull();
    fireEvent.click(screen.getByTestId("agent-view-context-size"));
    expect(screen.queryByTestId("run-timeline-row-context")).toBeNull();
  });

  it("has no context row for an agent no request has read", async () => {
    getRunTimeline.mockResolvedValue({ ...lifetimeResponse, units: lifetimeResponse.units.map((u) => ({ ...u, context_tokens: null, context_total: null, request: null })) });
    render();
    await screen.findByTestId("run-timeline-chart");
    expect(screen.queryByTestId("run-timeline-row-context")).toBeNull();
  });
});
