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
  boundaries: { initialize_turn: 1, last_before_compact_turn: 1 },
};

const labels = {
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
};

describe("RunTimelineChart", () => {
  it("renders independent panels with dashed correspondence connectors and event rail", () => {
    const { container } = render(<RunTimelineChart timeline={timeline} labels={labels} />);

    expect(screen.getByLabelText("Time panel")).toBeTruthy();
    expect(screen.getByLabelText("Tokens panel")).toBeTruthy();
    expect(screen.getByLabelText("Event rail")).toBeTruthy();
    expect(container.querySelectorAll('[data-testid="run-connector"]')).toHaveLength(2);
    expect(container.querySelectorAll('[data-testid="run-connector"]')[0].getAttribute("stroke-dasharray")).toBe("4 4");
    expect(screen.getByText("compact")).toBeTruthy();
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
