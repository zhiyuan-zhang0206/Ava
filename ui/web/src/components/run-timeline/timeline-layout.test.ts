import { describe, expect, it } from "vitest";

import type { RunTimelineResponse } from "@/lib/types";

import { buildTimelineLayout } from "./timeline-layout";

const row: RunTimelineResponse["rows"][number] = {
  turn: 1,
  n_turns: 1,
  start: "2026-09-02T08:00:00Z",
  end: "2026-09-02T08:10:00Z",
  active_s: 120,
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
  execs: [],
  anomalies: [],
  tags: [],
};

describe("buildTimelineLayout", () => {
  it("projects turn geometry and event source nodes on one linear time scale", () => {
    const layout = buildTimelineLayout({
      width: 1000,
      window: { from: "2026-09-02T08:00:00Z", to: "2026-09-02T09:00:00Z" },
      rows: [row],
      events: [{ ts: "2026-09-02T08:30:00Z", kind: "compact", trace_id: null, label: null }],
    });

    expect(layout.plot).toEqual({ left: 32, right: 968, width: 936 });
    expect(layout.ticks.map((tick) => tick.x)).toEqual([32, 266, 500, 734, 968]);
    expect(layout.ticks[2].timestamp).toBe("2026-09-02T08:30:00.000Z");
    expect(layout.turns[0]).toMatchObject({ projectedStartX: 32, projectedEndX: 188, left: 32, width: 156 });
    expect(layout.events[0].source).toEqual({ x: 500, y: 38 });
    expect(layout.events[0].destination).toEqual({ x: 500, y: 54 });
  });

  it("moves colliding event chips to another lane without detaching either connector endpoint", () => {
    const layout = buildTimelineLayout({
      width: 1000,
      window: { from: "2026-09-02T08:00:00Z", to: "2026-09-02T09:00:00Z" },
      rows: [row],
      events: [
        { ts: "2026-09-02T08:00:00Z", kind: "compact", trace_id: null, label: null },
        { ts: "2026-09-02T08:00:00Z", kind: "exec_failed", trace_id: "trace-1", label: null },
      ],
    });

    expect(layout.events.map((event) => event.lane)).toEqual([0, 1]);
    expect(layout.events[0].source).toEqual({ x: 32, y: 38 });
    expect(layout.events[0].destination).toEqual({ x: 60, y: 54 });
    expect(layout.connectors[0]).toMatchObject({
      source: { x: 32, y: 38 },
      destination: { x: 60, y: 54 },
    });
    expect(layout.connectors[0].path).toBe("M 32 38 C 32 46 60 46 60 54");
    expect(layout.connectors[1].source).toEqual(layout.events[1].source);
    expect(layout.connectors[1].destination).toEqual(layout.events[1].destination);
  });
});
