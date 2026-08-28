import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { RunTimelineResponse } from "@/lib/types";

import { RunTimelineChart } from "./run-timeline-chart";
import { tokenBarWidth } from "./scales";

afterEach(cleanup);

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
      tags: ["compact@2026-08-29T08:00:06Z"],
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
  waterfall: "Timeline waterfall",
  time: "Time",
  tokens: "Tokens",
  eventRail: "Event rail",
  input: "Input",
  output: "Output",
  idle: "Idle",
  turn: "Turn",
  bucket: "Bucket",
  cost: "Cost",
  model: "Model",
  empty: "No activity in this window.",
  noTokenData: "No token data is available for this window.",
  moreEvents: (count: number, summary: string) => `+${count} more (${summary})`,
};

describe("RunTimelineChart", () => {
  it("renders independent panels with dashed correspondence connectors and event rail", () => {
    const { container } = render(<RunTimelineChart timeline={timeline} labels={labels} />);

    expect(screen.getByLabelText("Timeline chart")).toBeTruthy();
    expect(screen.getByLabelText("Timeline waterfall")).toBeTruthy();
    expect(screen.getByLabelText("Time panel")).toBeTruthy();
    expect(screen.getByLabelText("Tokens panel")).toBeTruthy();
    expect(screen.getByLabelText("Event rail")).toBeTruthy();
    expect(container.querySelectorAll('[data-testid="run-connector"]')).toHaveLength(2);
    expect(container.querySelectorAll('[data-testid="run-connector"]')[0].getAttribute("stroke-dasharray")).toBe("4 4");
    expect(screen.getByText("compact")).toBeTruthy();
  });

  it("uses theme-aware SVG stroke classes for connectors and anomalous turn bars", () => {
    const { container } = render(<RunTimelineChart timeline={timeline} labels={labels} />);

    const connector = container.querySelector('[data-testid="run-connector"]');
    const firstTimeBar = container.querySelector('[aria-label="Time panel"] rect');

    expect(connector?.getAttribute("stroke")).toBeNull();
    expect(connector?.getAttribute("class")).toContain("stroke-muted-foreground");
    expect(firstTimeBar?.getAttribute("stroke")).toBeNull();
    expect(firstTimeBar?.getAttribute("class")).toContain("stroke-destructive");
  });

  it("skips idle labels that would collide with the next turn label", () => {
    const crowdedTimeline: RunTimelineResponse = {
      ...timeline,
      rows: [
        { ...timeline.rows[0], tags: ["idle_before_61s"] },
        {
          ...timeline.rows[1],
          turn: 2,
          start: "2026-08-29T08:01:00Z",
          end: "2026-08-29T08:01:04Z",
          tags: ["idle_before_121s"],
        },
        {
          ...timeline.rows[1],
          turn: 3,
          start: "2026-08-29T08:02:00Z",
          end: "2026-08-29T08:02:04Z",
          tags: ["idle_before_181s"],
        },
      ],
    };

    render(<RunTimelineChart timeline={crowdedTimeline} labels={labels} />);

    expect(screen.queryByText("Idle 1m")).toBeNull();
    expect(screen.queryByText("Idle 2m")).toBeNull();
    expect(screen.getByText("Idle 3m")).toBeTruthy();
  });

  it("caps the event rail at 120 colored chips", () => {
    const events: RunTimelineResponse["events"] = [
      { ts: "2026-08-29T08:00:00Z", kind: "compact", trace_id: null, label: "compact" },
      { ts: "2026-08-29T08:00:01Z", kind: "restart_completed", trace_id: null, label: "restart" },
      { ts: "2026-08-29T08:00:02Z", kind: "exec_failed", trace_id: null, label: "failed" },
      ...Array.from({ length: 119 }, (_, index) => ({
        ts: `2026-08-29T08:${String(index).padStart(2, "0")}:00Z`,
        kind: "exec",
        trace_id: null,
        label: null,
      })),
    ];
    const { container } = render(<RunTimelineChart timeline={{ ...timeline, events }} labels={labels} />);

    expect(container.querySelectorAll('[data-testid="event-chip"]')).toHaveLength(120);
    expect(screen.getByText("+2 more (exec×2)")).toBeTruthy();
    expect(screen.getByText("compact").getAttribute("class")).toContain("border-violet-500/50");
    expect(screen.getByText("restart_completed").getAttribute("class")).toContain("border-blue-500/50");
    expect(screen.getByText("exec_failed").getAttribute("class")).toContain("border-destructive/50");
  });

  it("prioritizes rare rail events before ordinary exec chips and summarizes skipped kinds", () => {
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

  it("keeps a dense token panel within a readable capped height", () => {
    const denseTimeline: RunTimelineResponse = {
      ...timeline,
      rows: Array.from({ length: 157 }, (_, index) => ({
        ...timeline.rows[0],
        turn: index + 1,
        start: `2026-08-29T08:${String(index % 60).padStart(2, "0")}:00Z`,
        end: `2026-08-29T08:${String(index % 60).padStart(2, "0")}:01Z`,
      })),
    };
    const { container } = render(<RunTimelineChart timeline={denseTimeline} labels={labels} />);
    const svg = container.querySelector("svg");

    expect(Number(svg?.getAttribute("viewBox")?.split(" ").at(-1))).toBeGreaterThan(286);
  });

  it("scrolls the wide SVG inside the chart instead of overflowing the page", () => {
    render(<RunTimelineChart timeline={timeline} labels={labels} />);

    expect(screen.getByTestId("run-timeline-scroll").className).toContain("overflow-x-auto");
  });

  it("replaces empty axes with an activity hint", () => {
    render(<RunTimelineChart timeline={{ ...timeline, rows: [] }} labels={labels} />);

    expect(screen.getByText("No activity in this window.")).toBeTruthy();
  });

  it("explains when every displayed turn lacks token data", () => {
    render(
      <RunTimelineChart
        timeline={{
          ...timeline,
          meta: { ...timeline.meta, n_turns: 2, tokens_in: 0, tokens_out: 0, unmatched_turns: 2 },
          rows: timeline.rows.map((row) => ({
            ...row,
            llm: { ...row.llm, calls: 0, in_total: 0, out_total: 0 },
          })),
        }}
        labels={labels}
      />,
    );

    expect(screen.getByText("No token data is available for this window.")).toBeTruthy();
  });

  it("shows the absolute token and cost detail on hover", () => {
    render(<RunTimelineChart timeline={timeline} labels={labels} />);

    fireEvent.mouseEnter(screen.getByLabelText("Turn 1 token bar"));

    expect(screen.getByRole("tooltip").textContent).toContain("120");
    expect(screen.getByRole("tooltip").textContent).toContain("deepseek-v4-flash");
  });
});

describe("tokenBarWidth", () => {
  it("maps raw absolute token counts onto only the token panel scale", () => {
    expect(tokenBarWidth(120, 240, 300)).toBe(150);
    expect(tokenBarWidth(0, 240, 300)).toBe(0);
  });
});
