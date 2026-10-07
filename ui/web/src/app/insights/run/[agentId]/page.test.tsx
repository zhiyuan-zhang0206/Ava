import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ContextBreakdownResponse,
  RunTimelineMessages,
  RunTimelineResponse,
  UserSettingListResponse,
} from "@/lib/types";

const { getRunTimeline, getRunTimelineMessages, getSettings, getContextBreakdown, useMediaQuery } =
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
  }));

vi.mock("@/lib/use-media-query", () => ({ useMediaQuery }));

vi.mock("@/lib/api", () => ({
  api: { getRunTimeline, getRunTimelineMessages, getSettings, getContextBreakdown },
}));

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
    },
  ],
  events: [{ ts: "2026-10-04T12:00:00.000000Z", kind: "spawn", label: null }],
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
  estimated_total: 250,
  max_input_tokens: 1_000_000,
  soft_compact_tokens: 374_000,
  hard_compact_tokens: 512_000,
  sections: [{ name: "(preamble)", tokens: 100 }],
  categories: [{ kind: "system_prompt", tokens: 400 }],
};

function render() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(
    <QueryClientProvider client={queryClient}>
      <RunTimelinePage params={Promise.resolve({ agentId: "42" })} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useMediaQuery.mockReturnValue(false);
  getRunTimeline.mockReset();
  getRunTimeline.mockResolvedValue(lifetimeResponse);
  getRunTimelineMessages.mockReset();
  getRunTimelineMessages.mockResolvedValue(messagesResponse);
  getSettings.mockReset();
  getSettings.mockResolvedValue({ settings: [] });
  getContextBreakdown.mockReset();
  getContextBreakdown.mockResolvedValue(cbdFixture);
});

afterEach(() => {
  vi.useRealTimers();
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
    ]);
    expect(screen.getAllByTestId("run-timeline-node")).toHaveLength(3);
    expect(screen.getAllByTestId("run-timeline-unit")).toHaveLength(3);
    expect(screen.getAllByTestId("run-timeline-event")).toHaveLength(1);
    expect(screen.getByTestId("run-timeline-window").textContent).toContain("3 summary nodes");
  });

  it("has no compare entry and no session switch", async () => {
    render();
    await screen.findByTestId("run-timeline-chart");
    expect(screen.queryByRole("link", { name: "Compare agents" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Compact session" })).toBeNull();
  });

  it("keeps the context breakdown card", async () => {
    render();
    await waitFor(() => expect(getContextBreakdown).toHaveBeenCalledWith(42));
  });
});

describe("ancestors", () => {
  const highlight = async (id: string) =>
    (await screen.findAllByTestId("run-timeline-node"))
      .find((candidate) => candidate.getAttribute("data-node-id") === id)!
      .getAttribute("data-highlight");

  it("lights a node and its ancestors, and steps the others back", async () => {
    render();
    fireEvent.click((await screen.findAllByTestId("run-timeline-node")).find(
      (candidate) => candidate.getAttribute("data-node-id") === "1",
    )!);
    expect(await highlight("1")).toBe("self");
    expect(await highlight("3")).toBe("ancestor");
    expect(await highlight("2")).toBe("none");
  });

  it("lights a message block's covering leaf and every ancestor above it", async () => {
    render();
    const unit = (await screen.findAllByTestId("run-timeline-unit")).find(
      (candidate) => candidate.getAttribute("data-unit-kind") === "text",
    )!;
    fireEvent.click(unit);
    expect(unit.getAttribute("data-highlight")).toBe("self");
    expect(await highlight("1")).toBe("ancestor");
    expect(await highlight("3")).toBe("ancestor");
    expect(await highlight("2")).toBe("none");
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
    const node = (await screen.findAllByTestId("run-timeline-node")).find(
      (candidate) => candidate.getAttribute("data-node-id") === "1",
    )!;
    fireEvent.click(node);

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
    const node = (await screen.findAllByTestId("run-timeline-node")).find(
      (candidate) => candidate.getAttribute("data-node-id") === "2",
    )!;
    fireEvent.click(node);
    expect((await screen.findByTestId("run-timeline-node-detail")).textContent).toContain(
      "No understanding-call record for this node.",
    );
  });

  it("reads a unit's own raw parts: a text unit shows the text, not the thinking or the call", async () => {
    render();
    const unit = (await screen.findAllByTestId("run-timeline-unit")).find(
      (candidate) => candidate.getAttribute("data-unit-kind") === "text",
    )!;
    fireEvent.click(unit);

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

describe("drilling", () => {
  async function node(id: string) {
    return (await screen.findAllByTestId("run-timeline-node")).find(
      (candidate) => candidate.getAttribute("data-node-id") === id,
    )!;
  }
  async function doubleClickNode(id: string) {
    fireEvent.doubleClick(await node(id));
  }

  it("zooms to the node's span and adds a crumb, without another read", async () => {
    render();
    const before = await screen.findByTestId("run-timeline-window");
    const whole = before.textContent;
    await doubleClickNode("1");
    const crumbs = await screen.findByTestId("run-timeline-crumbs");
    expect(crumbs.textContent).toContain("Level 1 · The agent read the repo");
    expect(summaryText()).not.toBe(whole);
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
    // node 2 starts after node 1 ends: it is outside the zoomed view
    expect(screen.getAllByTestId("run-timeline-node").map((n) => n.getAttribute("data-node-id"))).not.toContain("2");
    // the double-click also selected the node
    expect(await screen.findByTestId("run-timeline-node-detail")).toBeTruthy();
  });

  it("drills through the Drill button of the side panel too", async () => {
    render();
    fireEvent.click(await node("3"));
    fireEvent.click(await screen.findByRole("button", { name: "Drill in" }));
    expect(screen.getByTestId("run-timeline-crumbs").textContent).toContain("Level 2");
  });

  it("a single click selects and does not drill", async () => {
    render();
    fireEvent.click(await node("1"));
    await screen.findByTestId("run-timeline-node-detail");
    expect(screen.getByTestId("run-timeline-crumbs").textContent).not.toContain("Level 1");
  });

  it("steps back one level at a time, and to the whole lifetime from the root", async () => {
    render();
    const whole = (await screen.findByTestId("run-timeline-window")).textContent;
    await doubleClickNode("3");
    const level2 = summaryText();
    await doubleClickNode("1");
    expect(summaryText()).not.toBe(level2);

    const crumbs = screen.getByTestId("run-timeline-crumbs");
    fireEvent.click(within(crumbs).getByRole("button", { name: /Level 2/ }));
    expect(summaryText()).toBe(level2);
    expect(within(screen.getByTestId("run-timeline-crumbs")).queryByText(/Level 1/)).toBeNull();

    fireEvent.click(within(screen.getByTestId("run-timeline-crumbs")).getByRole("button", { name: "Whole lifetime" }));
    expect(summaryText()).toBe(whole);
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it("clears the drill path when the resolved agentId changes in place", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender } = rtlRender(
      <QueryClientProvider client={queryClient}>
        <RunTimelinePage params={Promise.resolve({ agentId: "42" })} />
      </QueryClientProvider>,
    );
    await doubleClickNode("1");
    await waitFor(() =>
      expect(screen.getByTestId("run-timeline-crumbs").textContent).toContain("Level 1"),
    );

    rerender(
      <QueryClientProvider client={queryClient}>
        <RunTimelinePage params={Promise.resolve({ agentId: "43" })} />
      </QueryClientProvider>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("run-timeline-crumbs").textContent).not.toContain("Level 1"),
    );
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
    const unit = screen.getAllByTestId("run-timeline-unit")[0];
    fireEvent.pointerDown(unit, { clientX: 400, button: 0, pointerId: 1 });
    fireEvent.pointerMove(unit, { clientX: 200, pointerId: 1 });
    fireEvent.pointerUp(unit, { clientX: 200, pointerId: 1 });
    fireEvent.click(unit);
    expect(summaryText()).not.toBe(zoomed);
    expect(screen.queryByTestId("run-timeline-unit-detail")).toBeNull();
  });

  it("a press that barely moves is still a click", async () => {
    render();
    const unit = (await screen.findAllByTestId("run-timeline-unit"))[0];
    fireEvent.pointerDown(unit, { clientX: 400, button: 0, pointerId: 1 });
    fireEvent.pointerUp(unit, { clientX: 401, pointerId: 1 });
    fireEvent.click(unit);
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
    getRunTimeline.mockResolvedValue({
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
        },
      ],
    });
    render();
    const blocks = await screen.findAllByTestId("run-timeline-unit");
    fireEvent.click(blocks.find((b) => b.getAttribute("data-unit-kind") === "thinking")!);
    let detail = await screen.findByTestId("run-timeline-unit-detail");
    expect(await within(detail).findByText("need a plan", { selector: "pre" })).toBeTruthy();
    expect(within(detail).queryByText("ls")).toBeNull();
    expect(within(detail).queryByText("on it")).toBeNull();

    fireEvent.click(blocks.find((b) => b.getAttribute("data-unit-kind") === "call")!);
    detail = await screen.findByTestId("run-timeline-unit-detail");
    expect(await within(detail).findByText("ls", { selector: "pre" })).toBeTruthy();
    expect(within(detail).queryByText("need a plan")).toBeNull();
  });

  it("draws every block on one lane and lists the block colors in a legend", async () => {
    render();
    const blocks = await screen.findAllByTestId("run-timeline-unit");
    expect(blocks.every((block) => block.style.top === "")).toBe(true);
    const legend = screen.getByTestId("run-timeline-legend");
    expect(within(legend).getAllByRole("listitem")).toHaveLength(7);
  });
});
