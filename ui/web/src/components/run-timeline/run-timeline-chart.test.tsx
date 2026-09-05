import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import fixture3187Json from "../../../fixtures/run-timeline-3187.json";
import fixture405Json from "../../../fixtures/run-timeline-405.json";
import type { RunTimelineResponse } from "@/lib/types";

import { RunTimelineChart } from "./run-timeline-chart";

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
        model: "deepseek-v4-flash",
      },
      execs: [{ tool: "execute_code", dur_s: 0, ok: false }],
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
        model: "deepseek-v4-flash",
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
  duration: "Duration",
  status: "Status",
  succeeded: "Succeeded",
  failed: "Failed",
  anomalies: "Anomalies",
  none: "None",
  noExecutions: "No executions",
  closeDetails: "Close details",
  eventDetails: "Event details",
  kind: "Kind",
  timestamp: "Timestamp",
  detail: "Detail",
};

afterEach(() => vi.restoreAllMocks());

describe("RunTimelineChart", () => {
  it("renders every turn as one clickable block on a single linear track", () => {
    const { container } = render(<RunTimelineChart timeline={timeline} labels={labels} />);

    expect(screen.getByLabelText("Timeline chart")).toBeTruthy();
    expect(screen.getByLabelText("Timeline visualization")).toBeTruthy();
    expect(container.querySelectorAll('[data-testid="turn-block"]')).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Turn 1" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Turn 2" })).toBeTruthy();
    expect(screen.queryByLabelText("Tokens panel")).toBeNull();
  });

  it("keeps every glyph in a fixed sibling overlay outside transformable geometry", () => {
    const { container } = render(<RunTimelineChart timeline={timeline} labels={labels} />);
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
    const { container } = render(<RunTimelineChart timeline={subMinuteTimeline} labels={labels} />);

    const tickLabels = Array.from(container.querySelectorAll("[data-timeline-tick]"), (tick) => tick.textContent);
    expect(tickLabels).toEqual(["08:00:00", "08:00:10", "08:00:20", "08:00:30", "08:00:40"]);
    expect(tickLabels).toEqual([...new Set(tickLabels)]);
  });

  it("includes seconds when a minute-plus window still has sub-minute tick spacing", () => {
    const ninetySecondTimeline = {
      ...timeline,
      window: { from: "2026-08-29T08:00:00Z", to: "2026-08-29T08:01:30Z" },
    };
    const { container } = render(<RunTimelineChart timeline={ninetySecondTimeline} labels={labels} />);

    const tickLabels = Array.from(container.querySelectorAll("[data-timeline-tick]"), (tick) => tick.textContent);
    expect(tickLabels).toEqual(["08:00:00", "08:00:22", "08:00:45", "08:01:07", "08:01:30"]);
    expect(tickLabels).toEqual([...new Set(tickLabels)]);
  });

  it("keeps minute-only tick labels for the one-hour window", () => {
    const { container } = render(<RunTimelineChart timeline={timeline} labels={labels} />);

    const tickLabels = Array.from(container.querySelectorAll("[data-timeline-tick]"), (tick) => tick.textContent);
    expect(tickLabels).toEqual(["08:00", "08:15", "08:30", "08:45", "09:00"]);
  });

  it("anchors connector paths to the exact source and destination node coordinates", () => {
    const { container } = render(<RunTimelineChart timeline={timeline} labels={labels} />);
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
    render(<RunTimelineChart timeline={timeline} labels={labels} />);

    fireEvent.click(screen.getByRole("button", { name: "Turn 1" }));
    const panel = screen.getByRole("region", { name: "Turn details" });

    expect(within(panel).getByText("2026-08-29 08:00:00 – 08:00:04")).toBeTruthy();
    expect(within(panel).getByText("2.0s")).toBeTruthy();
    expect(within(panel).getByText("120")).toBeTruthy();
    expect(within(panel).getByText("12")).toBeTruthy();
    expect(within(panel).getByText("$0.02")).toBeTruthy();
    expect(within(panel).getByText("deepseek-v4-flash")).toBeTruthy();
    expect(within(panel).getByText("1.50s")).toBeTruthy();
    expect(within(panel).getByText("execute_code")).toBeTruthy();
    expect(within(panel).getByText("0.00s")).toBeTruthy();
    expect(within(panel).getAllByText("Failed")).toHaveLength(2);
    expect(within(panel).getByText("exec_failed")).toBeTruthy();
  });

  it("shows turn facts on hover without breaking click-to-select", () => {
    render(<RunTimelineChart timeline={timeline} labels={labels} />);

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
  });

  it("shows the full event label on hover and focus through one shared popover", () => {
    render(<RunTimelineChart timeline={timeline} labels={labels} />);

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
    const { container } = render(<RunTimelineChart timeline={fixture} labels={labels} />);

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
    const { container } = render(<RunTimelineChart timeline={{ ...timeline, events }} labels={labels} />);

    expect(container.querySelectorAll('[data-testid="event-chip"]')).toHaveLength(120);
    expect(screen.getByText("compact")).toBeTruthy();
    expect(screen.getByText("+2 more (exec×2)")).toBeTruthy();
  });

  it.each([
    ["3187", fixture3187Json as RunTimelineResponse],
    ["405", fixture405Json as RunTimelineResponse],
  ])("renders all real turn rows from fixture %s", (_agentId, fixture) => {
    const { container } = render(<RunTimelineChart timeline={fixture} labels={labels} />);

    expect(container.querySelectorAll('[data-testid="turn-block"]')).toHaveLength(fixture.rows.length);
    expect(container.querySelectorAll('[data-testid="event-chip"]')).toHaveLength(fixture.events.length);
  });

  it("replaces an empty track with an activity hint", () => {
    render(<RunTimelineChart timeline={{ ...timeline, rows: [] }} labels={labels} />);

    expect(screen.getByText("No activity in this window.")).toBeTruthy();
  });
});
