import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import fixture3187Json from "../../../fixtures/run-timeline-3187.json";
import fixture405Json from "../../../fixtures/run-timeline-405.json";
import type { RunTimelineResponse } from "@/lib/types";

import { RunTimelineChart } from "./run-timeline-chart";

// jsdom has no PointerEvent; the drag tests need pointer coordinates, so give
// pointer events a MouseEvent body (P4-1, task #4023).
if (typeof window !== "undefined" && typeof window.PointerEvent === "undefined") {
  Object.defineProperty(window, "PointerEvent", {
    value: class PointerEventPolyfill extends MouseEvent {},
    configurable: true,
  });
}

const timeline: RunTimelineResponse = {
  agent_id: 405,
  window: { from: "2026-08-29T08:00:00Z", to: "2026-08-29T09:00:00Z" },
  meta: {
    n_turns: 2,
    wall_span_s: 3600,
    active_s: 8,
    tokens_in: 360,
    tokens_out: 36,
    cost_usd: 0.02,
    n_exec_failed: 1,
    n_compact: 1,
    n_restart: 0,
    fallback_turns: 0,
    unmatched_turns: 0,
  },
  rows: [
    {
      turn: 1,
      n_turns: 1,
      start: "2026-08-29T08:00:00Z",
      end: "2026-08-29T08:00:04Z",
      active_s: 2,
      trace_id: "trace-1",
      checkpoint_id: null,
      ok: true,
      llm: {
        calls: 1,
        in_total: 120,
        cache_read: 100,
        out_total: 12,
        reasoning: 4,
        latency_ms: 1500,
        cost_usd: 0.02,
        model: "deepseek-flash",
      },
      execs: [{ tool: "execute_code", ok: false }],
      anomalies: ["exec_failed"],
      tags: [],
    },
    {
      turn: 2,
      n_turns: 1,
      start: "2026-08-29T08:30:00Z",
      end: "2026-08-29T08:30:04Z",
      active_s: 3,
      trace_id: "trace-2",
      checkpoint_id: null,
      ok: true,
      llm: {
        calls: 1,
        in_total: 240,
        cache_read: 200,
        out_total: 24,
        reasoning: 8,
        latency_ms: 2500,
        cost_usd: 0,
        model: "deepseek-flash",
      },
      execs: [],
      anomalies: [],
      tags: ["idle_before_1796s"],
    },
  ],
  events: [
    { ts: "2026-08-29T08:00:06Z", kind: "compact", trace_id: null, label: null },
    { ts: "2026-08-29T08:00:03Z", kind: "exec_failed", trace_id: "trace-1", label: "ValueError" },
  ],
  boundaries: {
    initialize_turn: 1,
    last_before_compact_turn: 1,
    post_window_turns: 0,
    has_activity_after_window: false,
  },
};

const labels = {
  chart: "Timeline chart",
  visualization: "Timeline visualization",
  time: "Time",
  eventRail: "Event rail",
  input: "Input",
  output: "Output",
  turn: "Turn",
  bucket: "Bucket",
  cost: "Cost",
  model: "Model",
  empty: "No activity in this window.",
  moreEvents: (count: number, summary: string) => `+${count} more (${summary})`,
  turnDetails: "Turn details",
  timeRange: "Time range",
  activeSeconds: "Active seconds",
  latency: "Latency",
  executions: "Executions",
  tool: "Tool",
  status: "Status",
  succeeded: "Succeeded",
  failed: "Failed",
  anomalies: "Anomalies",
  none: "None",
  noExecutions: "No executions",
  closeDetails: "Close details",
  eventDetails: "Event details",
  layerDetails: "Layer details",
  layerSummary: "Summary",
  pendingLabel: "Pending",
  pendingExplainer:
    "This stretch has no understanding layer yet — it appears after the next seal (compact); history is not backfilled.",
  pendingAria: "Pending layer segment",
  showMore: "Show more",
  showLess: "Show less",
  kind: "Kind",
  timestamp: "Timestamp",
  detail: "Detail",
  crumbRoot: "Initial window",
  readoutIdle: "Wheel / drag to pan · Ctrl+wheel to zoom · hover for info · click to read",
};
const chartActions = {
  onDrillBucket: vi.fn(),
  onZoomWindow: vi.fn(),
};

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("RunTimelineChart", () => {
  it("renders every turn as one clickable block on a single linear track", () => {
    const { container } = render(<RunTimelineChart timeline={timeline} labels={labels} {...chartActions} />);

    expect(screen.getByLabelText("Timeline chart")).toBeTruthy();
    expect(screen.getByLabelText("Timeline visualization")).toBeTruthy();
    expect(container.querySelectorAll('[data-testid="turn-block"]')).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Turn 1" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Turn 2" })).toBeTruthy();
    expect(screen.queryByLabelText("Tokens panel")).toBeNull();
  });

  it("pins one canvas width when the compare view overrides it", () => {
    render(
      <RunTimelineChart timeline={timeline} labels={labels} {...chartActions} widthOverride={777} />,
    );
    const visualization = screen.getByTestId("run-timeline-visualization");
    expect(visualization.getAttribute("style")).toContain("width: 777px");

    fireEvent.click(screen.getByRole("button", { name: "Turn 1" }));
    // Opening the detail panel must not re-scale this lane: the compare view
    // owns one width for every lane.
    expect(screen.getByTestId("run-timeline-visualization").getAttribute("style")).toContain(
      "width: 777px",
    );
    expect(screen.getByText("Turn details")).toBeTruthy();
  });

  it("keeps every glyph in a fixed sibling overlay outside transformable geometry", () => {
    const { container } = render(<RunTimelineChart timeline={timeline} labels={labels} {...chartActions} />);
    const geometry = screen.getByTestId("run-timeline-geometry");
    const fixedText = container.querySelectorAll('[data-testid="fixed-timeline-text"]');

    expect(geometry.querySelector("text")).toBeNull();
    expect(geometry.querySelector("[transform]")).toBeNull();
    expect(fixedText.length).toBeGreaterThan(0);
    for (const glyph of fixedText) {
      expect(geometry.contains(glyph)).toBe(false);
      expect(glyph.getAttribute("style")).toMatch(/(?:left|right): \d+px/);
      expect(glyph.getAttribute("style")).not.toContain("transform");
    }
  });

  it("includes seconds in sub-minute tick labels so consecutive labels stay unique", () => {
    const subMinuteTimeline = {
      ...timeline,
      window: { from: "2026-08-29T08:00:00Z", to: "2026-08-29T08:00:40Z" },
    };
    const { container } = render(<RunTimelineChart timeline={subMinuteTimeline} labels={labels} {...chartActions} />);

    const tickLabels = Array.from(container.querySelectorAll("[data-timeline-tick]"), (tick) => tick.textContent);
    expect(tickLabels).toEqual(["08:00:00", "08:00:10", "08:00:20", "08:00:30", "08:00:40"]);
    expect(tickLabels).toEqual([...new Set(tickLabels)]);
  });

  it("includes seconds when a minute-plus window still has sub-minute tick spacing", () => {
    const ninetySecondTimeline = {
      ...timeline,
      window: { from: "2026-08-29T08:00:00Z", to: "2026-08-29T08:01:30Z" },
    };
    const { container } = render(<RunTimelineChart timeline={ninetySecondTimeline} labels={labels} {...chartActions} />);

    const tickLabels = Array.from(container.querySelectorAll("[data-timeline-tick]"), (tick) => tick.textContent);
    expect(tickLabels).toEqual(["08:00:00", "08:00:22", "08:00:45", "08:01:07", "08:01:30"]);
    expect(tickLabels).toEqual([...new Set(tickLabels)]);
  });

  it("keeps minute-only tick labels for the one-hour window", () => {
    const { container } = render(<RunTimelineChart timeline={timeline} labels={labels} {...chartActions} />);

    const tickLabels = Array.from(container.querySelectorAll("[data-timeline-tick]"), (tick) => tick.textContent);
    expect(tickLabels).toEqual(["08:00", "08:15", "08:30", "08:45", "09:00"]);
  });

  it("anchors connector paths to the exact source and destination node coordinates", () => {
    const { container } = render(<RunTimelineChart timeline={timeline} labels={labels} {...chartActions} />);
    const connector = container.querySelector('[data-testid="event-connector"][data-event-index="0"]');
    const source = container.querySelector('[data-testid="event-source-node"][data-event-index="0"]');
    const destination = container.querySelector('[data-testid="event-destination-node"][data-event-index="0"]');
    const sourcePoint = `${source?.getAttribute("cx")} ${source?.getAttribute("cy")}`;
    const destinationPoint = `${destination?.getAttribute("cx")} ${destination?.getAttribute("cy")}`;

    expect(connector).toBeTruthy();
    expect(connector?.getAttribute("d")).toMatch(new RegExp(`^M ${sourcePoint} C `));
    expect(connector?.getAttribute("d")).toMatch(new RegExp(` ${destinationPoint}$`));
    expect(connector?.getAttribute("data-source-x")).toBe(source?.getAttribute("cx"));
    expect(connector?.getAttribute("data-source-y")).toBe(source?.getAttribute("cy"));
    expect(connector?.getAttribute("data-destination-x")).toBe(destination?.getAttribute("cx"));
    expect(connector?.getAttribute("data-destination-y")).toBe(destination?.getAttribute("cy"));
  });

  it("opens a turn detail panel containing every supported row field", () => {
    render(<RunTimelineChart timeline={timeline} labels={labels} {...chartActions} />);

    fireEvent.click(screen.getByRole("button", { name: "Turn 1" }));
    const panel = screen.getByRole("region", { name: "Turn details" });

    expect(within(panel).getByText("2026-08-29 08:00:00 – 08:00:04")).toBeTruthy();
    expect(within(panel).getByText("2.0s")).toBeTruthy();
    expect(within(panel).getByText("120")).toBeTruthy();
    expect(within(panel).getByText("12")).toBeTruthy();
    expect(within(panel).getByText("$0.02")).toBeTruthy();
    expect(within(panel).getByText("deepseek-flash")).toBeTruthy();
    expect(within(panel).getByText("1.50s")).toBeTruthy();
    expect(within(panel).getByText("execute_code")).toBeTruthy();
    expect(within(panel).queryByText("Duration")).toBeNull();
    expect(within(panel).queryByText("0.00s")).toBeNull();
    expect(within(panel).getAllByText("Failed")).toHaveLength(2);
    expect(within(panel).getByText("exec_failed")).toBeTruthy();
  });

  it("shows turn facts on hover without breaking click-to-select", () => {
    render(<RunTimelineChart timeline={timeline} labels={labels} {...chartActions} />);

    const turn = screen.getByRole("button", { name: "Turn 1" });
    fireEvent.pointerEnter(turn);

    const popover = screen.getByRole("tooltip");
    expect(within(popover).getByText("Turn 1")).toBeTruthy();
    expect(within(popover).getByText("2026-08-29 08:00:00 – 08:00:04")).toBeTruthy();
    expect(within(popover).getByText("2.0s")).toBeTruthy();
    expect(within(popover).getByText("Failed")).toBeTruthy();
    expect(within(popover).getByText("1")).toBeTruthy();
    expect(within(popover).getByText("$0.02")).toBeTruthy();

    fireEvent.click(turn);
    expect(screen.getByRole("region", { name: "Turn details" })).toBeTruthy();
    // The panel supersedes the hover card: the click dismisses the popover it
    // opened with (the pointer never left the block).
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("shows bucket facts and drills into the bucket instead of selecting it", () => {
    const bucketRow: RunTimelineResponse["rows"][number] = {
      ...timeline.rows[0],
      turn: null,
      n_turns: 12,
      end: "2026-08-29T08:05:00Z",
    };
    const onDrillBucket = vi.fn();
    render(
      <RunTimelineChart
        timeline={{ ...timeline, rows: [bucketRow] }}
        labels={labels}
        onDrillBucket={onDrillBucket}
        onZoomWindow={vi.fn()}
      />,
    );

    const bucket = screen.getByRole("button", { name: "Bucket (12)" });
    fireEvent.pointerEnter(bucket);
    expect(within(screen.getByRole("tooltip")).getByText("Bucket (12)")).toBeTruthy();

    fireEvent.click(bucket);
    expect(onDrillBucket).toHaveBeenCalledWith(bucketRow);
    expect(screen.queryByRole("region", { name: "Turn details" })).toBeNull();
  });

  it("pans on a plain wheel and zooms around the ctrl-wheel cursor (P4-1)", () => {
    const onZoomWindow = vi.fn();
    render(
      <RunTimelineChart
        timeline={timeline}
        labels={labels}
        onDrillBucket={vi.fn()}
        onZoomWindow={onZoomWindow}
      />,
    );
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-29T10:00:00Z"));
    // Flush the wheel pan's animation frame synchronously.
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
    const visualization = screen.getByRole("group", { name: "Timeline visualization" });
    vi.spyOn(visualization, "getBoundingClientRect").mockReturnValue({
      x: 100,
      y: 0,
      top: 0,
      right: 1100,
      bottom: 200,
      left: 100,
      width: 1000,
      height: 200,
      toJSON: () => ({}),
    });

    // A plain wheel pans the window — the chart-scoped preventDefault is what
    // keeps the page from scrolling underneath it.
    const plainWheel = new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      deltaY: -1,
    });
    Object.defineProperty(plainWheel, "clientX", { value: 366 });
    visualization.dispatchEvent(plainWheel);
    expect(plainWheel.defaultPrevented).toBe(true);
    expect(onZoomWindow).toHaveBeenCalledTimes(1);
    const panned = onZoomWindow.mock.calls[0][0] as { from: string; to: string };
    expect(Date.parse(panned.to) - Date.parse(panned.from)).toBe(3_600_000);
    expect(Date.parse(panned.from)).toBeLessThan(Date.parse(timeline.window.from));

    // Ctrl+wheel zooms in around the cursor; the cursor keeps its instant, so
    // the window start moves later.
    onZoomWindow.mockClear();
    const zoomWheel = new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      deltaY: -120,
    });
    Object.defineProperty(zoomWheel, "ctrlKey", { value: true });
    Object.defineProperty(zoomWheel, "clientX", { value: 366 });
    visualization.dispatchEvent(zoomWheel);
    expect(zoomWheel.defaultPrevented).toBe(true);
    expect(onZoomWindow).toHaveBeenCalledTimes(1);
    const zoomed = onZoomWindow.mock.calls[0][0] as { from: string; to: string };
    expect(Date.parse(zoomed.to) - Date.parse(zoomed.from)).toBeLessThan(3_600_000);
    expect(Date.parse(zoomed.from)).toBeGreaterThan(Date.parse(timeline.window.from));
  });

  it("shows the full event label on hover and focus through one shared popover", () => {
    render(<RunTimelineChart timeline={timeline} labels={labels} {...chartActions} />);

    const event = screen.getByRole("button", { name: /exec_failed/ });
    fireEvent.pointerEnter(event);

    let popover = screen.getByRole("tooltip");
    expect(within(popover).getByText("Event details")).toBeTruthy();
    expect(within(popover).getByText("exec_failed")).toBeTruthy();
    expect(within(popover).getByText("2026-08-29 08:00:03")).toBeTruthy();
    expect(within(popover).getByText("ValueError")).toBeTruthy();
    expect(event.getAttribute("aria-describedby")).toBe(popover.id);

    fireEvent.pointerEnter(screen.getByRole("button", { name: "Turn 2" }));
    expect(screen.getAllByRole("tooltip")).toHaveLength(1);

    fireEvent.focus(event);
    popover = screen.getByRole("tooltip");
    expect(within(popover).getByText("ValueError")).toBeTruthy();
    expect(event.getAttribute("aria-describedby")).toBe(popover.id);
  });

  it("reprojects the last tick and turn inside the visible chart when details open", async () => {
    const visibleWidth = 600;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const width = this.getAttribute("data-testid") === "run-timeline-scroll" ? visibleWidth : 0;
      return {
        x: 0,
        y: 0,
        top: 0,
        right: width,
        bottom: 600,
        left: 0,
        width,
        height: 600,
        toJSON: () => ({}),
      };
    });
    const fixture = fixture3187Json as RunTimelineResponse;
    const { container } = render(<RunTimelineChart timeline={fixture} labels={labels} {...chartActions} />);

    fireEvent.click(screen.getByRole("button", { name: "Turn 1" }));
    await waitFor(() => {
      expect(screen.getByRole("group", { name: "Timeline visualization" }).getAttribute("style")).toContain(
        `width: ${visibleWidth}px`,
      );
    });

    const lastTick = container.querySelectorAll<HTMLElement>("[data-timeline-tick]").item(4);
    const lastTurn = container.querySelectorAll<SVGRectElement>('[data-testid="turn-block"]').item(fixture.rows.length - 1);
    expect(Number.parseFloat(lastTick.style.left) + 72).toBeLessThanOrEqual(visibleWidth);
    expect(Number(lastTurn.getAttribute("x")) + Number(lastTurn.getAttribute("width"))).toBeLessThanOrEqual(
      visibleWidth,
    );
  });

  it("keeps priority events when the rail reaches its 120-chip bound", () => {
    const events: RunTimelineResponse["events"] = [
      ...Array.from({ length: 121 }, (_, index) => ({
        ts: `2026-08-29T08:${String(index % 60).padStart(2, "0")}:00Z`,
        kind: "exec",
        trace_id: null,
        label: null,
      })),
      { ts: "2026-08-29T09:00:00Z", kind: "compact", trace_id: null, label: null },
    ];
    const { container } = render(
      <RunTimelineChart timeline={{ ...timeline, events }} labels={labels} {...chartActions} />,
    );

    expect(container.querySelectorAll('[data-testid="event-chip"]')).toHaveLength(120);
    expect(screen.getByText("compact")).toBeTruthy();
    expect(screen.getByText("+2 more (exec×2)")).toBeTruthy();
  });

  it.each([
    ["3187", fixture3187Json as RunTimelineResponse],
    ["405", fixture405Json as RunTimelineResponse],
  ])("renders all real turn rows from fixture %s", (_agentId, fixture) => {
    const { container } = render(<RunTimelineChart timeline={fixture} labels={labels} {...chartActions} />);

    expect(container.querySelectorAll('[data-testid="turn-block"]')).toHaveLength(fixture.rows.length);
    expect(container.querySelectorAll('[data-testid="event-chip"]')).toHaveLength(fixture.events.length);
  });

  it("replaces an empty track with an activity hint", () => {
    render(<RunTimelineChart timeline={{ ...timeline, rows: [] }} labels={labels} {...chartActions} />);

    expect(screen.getByText("No activity in this window.")).toBeTruthy();
  });

  it("renders narrative-layer blocks and opens the layer panel on click", () => {
    const layeredTimeline: RunTimelineResponse = {
      ...timeline,
      layers: [
        { id: "L0#0", depth: 0, parent: null, start: "2026-08-29T08:00:00Z", end: "2026-08-29T09:00:00Z", summary: "overview text" },
        { id: "L1#0", depth: 1, parent: "L0#0", start: "2026-08-29T08:00:00Z", end: "2026-08-29T08:30:00Z", summary: "stage a" },
        { id: "L1#1", depth: 1, parent: "L0#0", start: "2026-08-29T08:30:00Z", end: "2026-08-29T09:00:00Z", summary: "stage b" },
      ],
    };
    const { container } = render(<RunTimelineChart timeline={layeredTimeline} labels={labels} {...chartActions} />);

    expect(container.querySelectorAll('[data-testid="layer-block"]')).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "Layer details L0#0" }));
    const panel = screen.getByRole("region", { name: "Layer details" });
    expect(panel).toBeTruthy();
    expect(within(panel).getByText("overview text")).toBeTruthy();
    expect(within(panel).getByText("L0 \u00b7 L0#0")).toBeTruthy();
  });

  it("draws pending placeholders and explains them without a detail panel", () => {
    const pendingTimeline: RunTimelineResponse = {
      ...timeline,
      pending: [{ start: "2026-08-29T08:10:00Z", end: "2026-08-29T08:55:00Z" }],
    };
    const onZoomWindow = vi.fn();
    const { container } = render(
      <RunTimelineChart
        timeline={pendingTimeline}
        labels={labels}
        onDrillBucket={vi.fn()}
        onZoomWindow={onZoomWindow}
      />,
    );

    // No sealed blocks in the window, yet the layer band renders (spec §5).
    expect(container.querySelectorAll('[data-testid="layer-block"]')).toHaveLength(0);
    expect(container.querySelectorAll('[data-testid="pending-block"]')).toHaveLength(1);

    const button = screen.getByRole("button", { name: "Pending layer segment" });
    fireEvent.pointerEnter(button);
    const popover = screen.getByRole("tooltip");
    expect(within(popover).getByText("Pending")).toBeTruthy();
    expect(within(popover).getByText(labels.pendingExplainer)).toBeTruthy();

    // Not selectable: no layer detail panel opens.
    fireEvent.click(button);
    expect(screen.queryByRole("region", { name: "Layer details" })).toBeNull();

    // Double-click zooms into the stretch instead.
    fireEvent.doubleClick(button);
    expect(onZoomWindow).toHaveBeenCalledWith({
      from: "2026-08-29T08:10:00.000Z",
      to: "2026-08-29T08:55:00.000Z",
    });
  });

  it("renders the raw-context summary band when only a summary is provided", () => {
    const summaryTimeline: RunTimelineResponse = { ...timeline, summary: { text: "raw context summary" } };
    render(<RunTimelineChart timeline={summaryTimeline} labels={labels} {...chartActions} />);

    expect(screen.getByTestId("raw-summary")).toBeTruthy();
    expect(screen.getByText("raw context summary")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(screen.getByRole("button", { name: "Show less" })).toBeTruthy();
  });

  it("hides the summary UI when showSummaries is false", () => {
    const layeredTimeline: RunTimelineResponse = {
      ...timeline,
      layers: [
        { id: "L0#0", depth: 0, parent: null, start: "2026-08-29T08:00:00Z", end: "2026-08-29T09:00:00Z", summary: "overview text" },
      ],
      summary: { text: "raw context summary" },
    };
    const { container } = render(
      <RunTimelineChart timeline={layeredTimeline} labels={labels} {...chartActions} showSummaries={false} />,
    );

    expect(container.querySelectorAll('[data-testid="layer-block"]')).toHaveLength(0);
    expect(screen.queryByTestId("raw-summary")).toBeNull();
  });

  it("suppresses the block click once a drag crosses the 4px threshold (P4-1)", () => {
    const onZoomWindow = vi.fn();
    render(
      <RunTimelineChart
        timeline={timeline}
        labels={labels}
        onDrillBucket={vi.fn()}
        onZoomWindow={onZoomWindow}
      />,
    );
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
    const visualization = screen.getByRole("group", { name: "Timeline visualization" });
    const turn = screen.getByRole("button", { name: "Turn 1" });

    // A drag longer than the threshold pans and swallows the click it releases.
    fireEvent.pointerDown(visualization, { button: 0, clientX: 100 });
    fireEvent.pointerMove(window, { clientX: 140 });
    fireEvent.pointerUp(window);
    fireEvent.click(turn);
    expect(onZoomWindow).toHaveBeenCalledTimes(1);
    const panned = onZoomWindow.mock.calls[0][0] as { from: string; to: string };
    expect(Date.parse(panned.to) - Date.parse(panned.from)).toBe(3_600_000);
    expect(screen.queryByRole("region", { name: "Turn details" })).toBeNull();

    // A press without movement still selects the block.
    fireEvent.pointerDown(visualization, { button: 0, clientX: 100 });
    fireEvent.pointerUp(window);
    fireEvent.click(turn);
    expect(screen.getByRole("region", { name: "Turn details" })).toBeTruthy();
    expect(onZoomWindow).toHaveBeenCalledTimes(1);
  });

  it("keeps a drag gesture alive across a mid-gesture parent re-render (P4-1 / #2887 review)", () => {
    const panBefore = vi.fn();
    const panAfter = vi.fn();
    const frames: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback: FrameRequestCallback) => {
      frames.push(callback);
      return frames.length;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    const flushFrames = () => {
      for (const callback of frames.splice(0)) callback(0);
    };

    const { rerender } = render(
      <RunTimelineChart
        timeline={timeline}
        labels={labels}
        onDrillBucket={vi.fn()}
        onZoomWindow={panBefore}
      />,
    );
    const visualization = screen.getByRole("group", { name: "Timeline visualization" });
    const turn = screen.getByRole("button", { name: "Turn 1" });

    fireEvent.pointerDown(visualization, { button: 0, clientX: 100 });
    fireEvent.pointerMove(window, { clientX: 140 });
    flushFrames();
    expect(panBefore).toHaveBeenCalledTimes(1);

    // The page re-creates its onZoomWindow closure on every render; a parent
    // re-render mid-gesture must not reset the gesture (PR #2887 review: the
    // effect-local draft reset there, applying one frame and leaking the rest).
    rerender(
      <RunTimelineChart
        timeline={timeline}
        labels={labels}
        onDrillBucket={vi.fn()}
        onZoomWindow={panAfter}
      />,
    );

    fireEvent.pointerMove(window, { clientX: 220 });
    flushFrames();
    expect(panAfter).toHaveBeenCalledTimes(1);
    expect(panBefore).toHaveBeenCalledTimes(1);

    fireEvent.pointerUp(window);
    // The drag's click is swallowed, and the grab cursor is gone.
    fireEvent.click(turn);
    expect(screen.queryByRole("region", { name: "Turn details" })).toBeNull();
    expect(visualization.className).not.toContain("cursor-grabbing");
  });

  it("keeps a persistent readout line and names the hovered block (P4-1)", () => {
    render(<RunTimelineChart timeline={timeline} labels={labels} {...chartActions} withReadout />);

    expect(screen.getByTestId("timeline-readout").textContent).toBe(labels.readoutIdle);
    fireEvent.focus(screen.getByRole("button", { name: "Turn 1" }));
    const text = screen.getByTestId("timeline-readout").textContent;
    expect(text).toContain("Turn 1");
    expect(text).toContain("$0.02");
  });

  it("renders the focus path only when a trail exists and reports crumb picks (P4-1)", () => {
    const onCrumbSelect = vi.fn();
    const { rerender } = render(
      <RunTimelineChart timeline={timeline} labels={labels} {...chartActions} withReadout />,
    );
    expect(screen.queryByTestId("timeline-crumbs")).toBeNull();

    rerender(
      <RunTimelineChart
        timeline={timeline}
        labels={labels}
        {...chartActions}
        withReadout
        trail={[{ label: "L1#7", from: "2026-08-29T08:00:00Z", to: "2026-08-29T08:30:00Z" }]}
        onCrumbSelect={onCrumbSelect}
      />,
    );
    const crumbs = screen.getByTestId("timeline-crumbs");
    expect(within(crumbs).getByText("Initial window")).toBeTruthy();
    fireEvent.click(within(crumbs).getByText("Initial window"));
    expect(onCrumbSelect).toHaveBeenCalledWith(-1);
    fireEvent.click(within(crumbs).getByText(/L1#7/));
    expect(onCrumbSelect).toHaveBeenCalledWith(0);
  });

  it("flips the layer stack order when flipLayers is set (P4-1)", () => {
    const layeredTimeline: RunTimelineResponse = {
      ...timeline,
      layers: [
        { id: "L0#0", depth: 0, parent: null, start: "2026-08-29T08:00:00Z", end: "2026-08-29T09:00:00Z", summary: "overview text" },
        { id: "L1#0", depth: 1, parent: "L0#0", start: "2026-08-29T08:00:00Z", end: "2026-08-29T08:30:00Z", summary: "stage a" },
        { id: "L1#1", depth: 1, parent: "L0#0", start: "2026-08-29T08:30:00Z", end: "2026-08-29T09:00:00Z", summary: "stage b" },
      ],
    };
    const { container, rerender } = render(
      <RunTimelineChart timeline={layeredTimeline} labels={labels} {...chartActions} />,
    );
    const rowY = () =>
      Array.from(container.querySelectorAll('[data-testid="layer-block"]'), (block) =>
        Number(block.getAttribute("y")),
      );
    const before = rowY();
    const rowTop = before[0];
    const nextRowTop = before[1];
    expect(before).toEqual([rowTop, nextRowTop, nextRowTop]);

    rerender(<RunTimelineChart timeline={layeredTimeline} labels={labels} {...chartActions} flipLayers />);
    // Depth 1 (two blocks) now rides the top row; depth 0 moves below it.
    expect(rowY()).toEqual([rowTop, rowTop, nextRowTop]);
  });

  it("routes double-click focus through onFocusWindow with a block label (P4-1)", () => {
    const onFocusWindow = vi.fn();
    const pendingTimeline: RunTimelineResponse = {
      ...timeline,
      pending: [{ start: "2026-08-29T08:10:00Z", end: "2026-08-29T08:55:00Z" }],
    };
    render(
      <RunTimelineChart
        timeline={pendingTimeline}
        labels={labels}
        onDrillBucket={vi.fn()}
        onZoomWindow={vi.fn()}
        onFocusWindow={onFocusWindow}
      />,
    );

    fireEvent.doubleClick(screen.getByRole("button", { name: "Pending layer segment" }));
    expect(onFocusWindow).toHaveBeenCalledWith(
      { from: "2026-08-29T08:10:00.000Z", to: "2026-08-29T08:55:00.000Z" },
      labels.pendingLabel,
    );
  });
});
