import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ContextBreakdownResponse,
  RunTimelineContext,
  RunTimelineMessages,
  RunTimelineResponse,
  UserSettingListResponse,
} from "@/lib/contracts/types";

const {
  getRunTimeline,
  getRunTimelineMessages,
  getRunTimelineContext,
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
  }));

vi.mock("@/lib/layout/use-media-query", () => ({ useMediaQuery }));

vi.mock("@/lib/transport/api", () => ({
  api: { getRunTimeline, getRunTimelineMessages, getRunTimelineContext, getSettings, getContextBreakdown },
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
} from "@/components/run-timeline/run-timeline-test-canvas";
import RunTimelinePage from "./page";
import Loading from "./loading";

const LIFETIME = { from: "2026-10-04T12:00:00.000000Z", to: "2026-10-04T16:00:00.000000Z" };
const LEAF_A = { from: "2026-10-04T12:00:00.123456Z", to: "2026-10-04T13:00:00.654321Z" };

const usage = { calls: 3, input: 3000, cache_read: 2400, output: 120 };

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
      usage: { calls: 1, input: 100, cache_read: 0, output: 10 },
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
      usage: { calls: 4, input: 3100, cache_read: 2400, output: 130 },
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
      preview: "please fix the bug",
      parent: "1",
      context_tokens: null,
      generation_tokens: null,
      estimated: null,
    },
    {
      kind: "output",
      i0: 2,
      i1: 3,
      start: "2026-10-04T12:05:00.000000Z",
      end: "2026-10-04T12:06:00.000000Z",
      source: null,
      preview: "look at the failing test",
      parent: "1",
      context_tokens: null,
      generation_tokens: null,
      estimated: null,
    },
    {
      kind: "text",
      i0: 2,
      i1: 2,
      start: "2026-10-04T12:05:00.000000Z",
      end: "2026-10-04T12:05:00.000000Z",
      source: null,
      preview: "on it",
      parent: "1",
      context_tokens: null,
      generation_tokens: null,
      estimated: null,
    },
  ],
  events: [{ ts: "2026-10-04T12:00:00.000000Z", kind: "spawn", label: null }],
  requests: [
    { idx: 2, ts: "2026-10-04T12:04:00.000000Z", session: 0, input_tokens: 1000, output_tokens: 50, added_tokens: 900, added_estimated: false, added_from: 0, added_to: 2 },
    { idx: 7, ts: "2026-10-04T14:00:00.000000Z", session: 1, input_tokens: 400, output_tokens: 20, added_tokens: 380, added_estimated: true, added_from: 3, added_to: 7 },
  ],
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
      <RunTimelinePage params={Promise.resolve({ agentId: "42" })} />
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
  getRunTimeline.mockResolvedValue(lifetimeResponse);
  getRunTimelineMessages.mockReset();
  getRunTimelineMessages.mockResolvedValue(messagesResponse);
  getSettings.mockReset();
  getSettings.mockResolvedValue({ settings: [] });
  getContextBreakdown.mockReset();
  getContextBreakdown.mockResolvedValue(cbdFixture);
  getRunTimelineContext.mockReset();
  getRunTimelineContext.mockImplementation((_agent, at) => {
    const request = lifetimeResponse.requests.find((candidate) => candidate.idx >= at) ?? lifetimeResponse.requests[1];
    return Promise.resolve({
      ...cbdFixture,
      categories: [
        { kind: "system_prompt", tokens: 300, estimated: false, exact_fraction: 1 },
        { kind: "user_input", tokens: 200, estimated: false, exact_fraction: 1 },
        { kind: "reasoning", tokens: 100, estimated: true, exact_fraction: 0 },
      ],
      request: request.idx,
      session: request.session,
      sessions: 2,
      ts: request.ts,
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
      "run-timeline-row-added",
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
    expect((await screen.findByTestId("context-breakdown-heading")).textContent).toContain("request #7 · session 2 of 2");
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

  it("marks the stretch the level above has not summarized", async () => {
    const [first, second, top] = lifetimeResponse.nodes;
    getRunTimeline.mockResolvedValue({
      ...lifetimeResponse,
      nodes: [
        { ...first, parent: "3" },
        { ...second, parent: null },
        { ...top, span_end: 5, end: LEAF_A.to },
      ],
    });
    render();
    const pending = await screen.findAllByTestId("run-timeline-pending");
    expect(pending).toHaveLength(1);
    expect(within(screen.getByTestId("run-timeline-row-level-2")).getByTestId("run-timeline-pending")).toBe(
      pending[0],
    );
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
    expect(detail.textContent).toContain("#1–#5 (5)");
    expect(detail.textContent).toContain("Agent cost over this span");
    expect(detail.textContent).toContain("Cost of generating this summary");
    expect(detail.textContent).toContain("31.5s");
    // the agent's own cost: 3 calls, 3.0k input, 2.4k cache read
    expect(within(detail).getAllByText("3.0k").length).toBeGreaterThan(0);
    expect(within(detail).getAllByText("2.4k").length).toBeGreaterThan(0);
  });

  it("says so when a node has no understanding-call record", async () => {
    render();
    clickItem(await nodeOf("2"));
    expect((await screen.findByTestId("run-timeline-node-detail")).textContent).toContain(
      "No understanding-call record for this node.",
    );
  });

  it("reads a unit's own raw parts: a text unit shows the text, not the thinking or the call", async () => {
    render();
    clickItem(await unitOf("text"));

    const detail = await screen.findByTestId("run-timeline-unit-detail");
    expect(within(detail).queryByRole("button", { name: /raw messages/i })).toBeNull();
    await waitFor(() =>
      expect(getRunTimelineMessages).toHaveBeenCalledWith(42, {
        start: 2,
        end: 2,
        limit: 50,
        full: false,
      }),
    );
    const raw = await within(detail).findByText("on it", { selector: "pre" });
    expect(raw).toBeTruthy();
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
    expect(await screen.findByText("Could not load the run timeline.")).toBeTruthy();
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
          preview: "need a plan",
          parent: "1",
          context_tokens: null,
          generation_tokens: null,
          estimated: null,
        },
        {
          kind: "call",
          i0: 2,
          i1: 2,
          start: "2026-10-04T12:05:00.000000Z",
          end: "2026-10-04T12:05:00.000000Z",
          source: null,
          preview: "ls",
          parent: "1",
          context_tokens: null,
          generation_tokens: null,
          estimated: null,
        },
      ],
    };
    getRunTimeline.mockResolvedValue(response);
    render();
    await screen.findByTestId("run-timeline-canvas-units");
    clickAt("units", itemX(response, "units", "uthinking-2-2"));
    let detail = await screen.findByTestId("run-timeline-unit-detail");
    expect(await within(detail).findByText("need a plan", { selector: "pre" })).toBeTruthy();
    expect(within(detail).queryByText("ls")).toBeNull();
    expect(within(detail).queryByText("on it")).toBeNull();

    clickAt("units", itemX(response, "units", "ucall-2-2"));
    detail = await screen.findByTestId("run-timeline-unit-detail");
    expect(await within(detail).findByText("ls", { selector: "pre" })).toBeTruthy();
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
      "run-timeline-canvas-added",
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
    expect(readout()).toContain("Human message · #1–#1 · read");
    expect(readout()).toContain("user · please fix the bug");
    leaveItem(human);
    expect(readout()).toBe(idle);
  });

  it("reads a node's level, span, summary first line and the agent's own usage", async () => {
    render();
    hoverItem(await nodeOf("1"));
    const text = readout();
    expect(text).toContain("Level 1");
    expect(text).toContain("#1–#5");
    expect(text).toContain("The agent read the repo");
    expect(text).not.toContain("and planned");
    expect(text).toContain("3 calls · 3.0k in · 120 out");
    expect(text).toContain("4.2k tokens (estimated)");
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
      expect(screen.getByTestId("context-breakdown-heading").textContent).toContain("request #2 · session 1 of 2"),
    );
    clickItem(await nodeOf("2"));
    await waitFor(() => expect(getRunTimelineContext).toHaveBeenLastCalledWith(42, 6));
    await waitFor(() =>
      expect(screen.getByTestId("context-breakdown-heading").textContent).toContain("request #7 · session 2 of 2"),
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
    // Zooming at the left edge leaves only the first request (12:04) in view.
    wheel(chart, { deltaY: -600, clientX: 100 });
    await waitFor(() => expect(getRunTimelineContext).toHaveBeenLastCalledWith(42, 2));
    vi.restoreAllMocks();
  });

  it("says so when the agent has made no request", async () => {
    getRunTimeline.mockResolvedValue({ ...lifetimeResponse, requests: [] });
    render();
    expect((await screen.findByTestId("context-breakdown-empty")).textContent).toContain("no LLM request");
    expect(getRunTimelineContext).not.toHaveBeenCalled();
  });
});

describe("context size row", () => {
  it("draws one bar per request, scaled to the largest input, and reads its value on hover", async () => {
    render();
    await screen.findByTestId("run-timeline-chart");
    await paintFrame();
    const bars = drawn("input").filter((d) => d.op === "fill");
    expect(bars).toHaveLength(2);
    expect(bars[1].h / bars[0].h).toBeCloseTo(0.4);
    // Session 0 is blue, session 1 amber.
    expect(bars[0].color).toContain("#3b82f6");
    expect(bars[1].color).toContain("#f59e0b");
    pointAt("input", bars[1].x + bars[1].w / 2);
    expect(screen.getByTestId("run-timeline-readout").textContent).toContain("LLM request #7 · session 2");
    expect(screen.getByTestId("run-timeline-readout").textContent).toContain("400 input tokens · added 380 (estimated)");
  });

  it("has no row for an agent that made no request", async () => {
    getRunTimeline.mockResolvedValue({ ...lifetimeResponse, requests: [] });
    render();
    await screen.findByTestId("run-timeline-chart");
    expect(screen.queryByTestId("run-timeline-row-context")).toBeNull();
    expect(screen.queryByTestId("run-timeline-row-added")).toBeNull();
  });
});
