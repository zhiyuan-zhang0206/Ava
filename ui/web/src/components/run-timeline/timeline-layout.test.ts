import { describe, expect, it } from "vitest";

import type { RunTimelineResponse } from "@/lib/types";

import { buildTimelineLayout, mergePendingSpans, plotGeometry } from "./timeline-layout";

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
    model: "deepseek-flash",
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
    expect(layout.ticks[2].key).toBe("2026-09-02T08:30:00.000Z");
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

  it("lays narrative-layer rows between the event rail and the turn track when layers exist", () => {
    const layout = buildTimelineLayout({
      width: 1000,
      window: { from: "2026-09-02T08:00:00Z", to: "2026-09-02T09:00:00Z" },
      rows: [row],
      events: [],
      layers: [
        { id: "L0#0", depth: 0, parent: null, start: "2026-09-02T08:00:00Z", end: "2026-09-02T09:00:00Z", summary: "overview" },
        { id: "L1#0", depth: 1, parent: "L0#0", start: "2026-09-02T08:00:00Z", end: "2026-09-02T08:30:00Z", summary: "stage a" },
        { id: "L1#1", depth: 1, parent: "L0#0", start: "2026-09-02T08:30:00Z", end: "2026-09-02T09:00:00Z", summary: "stage b" },
      ],
    });

    expect(layout.layerRows).toHaveLength(2);
    expect(layout.layerRows[0]).toMatchObject({ depth: 0, top: 62, height: 22 });
    expect(layout.layerRows[0].blocks).toEqual([{ nodeIndex: 0, left: 32, width: 936 }]);
    expect(layout.layerRows[1].blocks).toEqual([
      { nodeIndex: 1, left: 32, width: 468 },
      { nodeIndex: 2, left: 500, width: 468 },
    ]);
    expect(layout.track.top).toBe(136);
  });

  it("lays pending placeholders in the first layer row without touching sealed blocks", () => {
    const layout = buildTimelineLayout({
      width: 1000,
      window: { from: "2026-09-02T08:00:00Z", to: "2026-09-02T09:00:00Z" },
      rows: [row],
      events: [],
      layers: [
        { id: "L0#0", depth: 0, parent: null, start: "2026-09-02T08:00:00Z", end: "2026-09-02T08:30:00Z", summary: "first half" },
      ],
      pending: [{ start: "2026-09-02T08:30:00Z", end: "2026-09-02T09:00:00Z" }],
    });

    expect(layout.pendingRow).toEqual({ top: layout.layerRows[0].top, height: 22 });
    expect(layout.pendingBlocks).toEqual([
      {
        index: 0,
        start: "2026-09-02T08:30:00.000Z",
        end: "2026-09-02T09:00:00.000Z",
        left: 500,
        width: 468,
      },
    ]);
  });

  it("synthesizes a pending row when no layer rows exist and keeps the track below it", () => {
    const layout = buildTimelineLayout({
      width: 1000,
      window: { from: "2026-09-02T08:00:00Z", to: "2026-09-02T09:00:00Z" },
      rows: [row],
      events: [],
      pending: [{ start: "2026-09-02T08:10:00Z", end: "2026-09-02T08:40:00Z" }],
    });

    expect(layout.layerRows).toEqual([]);
    expect(layout.pendingRow).toEqual({ top: 62, height: 22 });
    expect(layout.pendingBlocks).toEqual([
      {
        index: 0,
        start: "2026-09-02T08:10:00.000Z",
        end: "2026-09-02T08:40:00.000Z",
        left: 188,
        width: 468,
      },
    ]);
    // The synthesized row pushes the turn track down by its height + gap.
    expect(layout.track.top).toBe(62 + 22 + 24);
  });

  it("keeps the turn track position unchanged when no layers are present", () => {
    const layout = buildTimelineLayout({
      width: 1000,
      window: { from: "2026-09-02T08:00:00Z", to: "2026-09-02T09:00:00Z" },
      rows: [row],
      events: [],
    });

    expect(layout.layerRows).toEqual([]);
    expect(layout.track.top).toBe(62);
  });

  it("flips the layer stack fine-first while placeholders keep their host row (P4-1)", () => {
    const layout = buildTimelineLayout({
      width: 1000,
      window: { from: "2026-09-02T08:00:00Z", to: "2026-09-02T09:00:00Z" },
      rows: [row],
      events: [],
      layers: [
        { id: "L0#0", depth: 0, parent: null, start: "2026-09-02T08:00:00Z", end: "2026-09-02T09:00:00Z", summary: "overview" },
        { id: "L1#0", depth: 1, parent: "L0#0", start: "2026-09-02T08:00:00Z", end: "2026-09-02T08:30:00Z", summary: "stage a" },
      ],
      pending: [{ start: "2026-09-02T08:30:00Z", end: "2026-09-02T09:00:00Z" }],
      flipLayers: true,
    });

    expect(layout.layerRows.map((layerRow) => layerRow.depth)).toEqual([1, 0]);
    expect(layout.layerRows[0].top).toBe(62);
    expect(layout.layerRows[1].top).toBe(62 + 22 + 6);
    // The placeholder host row is the pre-flip first row (depth 0): the
    // placeholder moved with its row instead of re-targeting another one.
    expect(layout.pendingRow).toEqual({ top: 62 + 22 + 6, height: 22 });
  });
});

describe("mergePendingSpans", () => {
  it("merges stretches whose gap is within two minutes and drops sub-three-minute slivers", () => {
    const spans = mergePendingSpans([
      { start: "2026-09-02T08:00:00Z", end: "2026-09-02T08:10:00Z" },
      { start: "2026-09-02T08:11:00Z", end: "2026-09-02T08:20:00Z" },
      { start: "2026-09-02T08:30:00Z", end: "2026-09-02T08:31:00Z" },
    ]);

    expect(spans).toEqual([
      { start: "2026-09-02T08:00:00.000Z", end: "2026-09-02T08:20:00.000Z" },
    ]);
  });

  it("keeps a stretch at exactly the three-minute floor and sorts out-of-order input", () => {
    const spans = mergePendingSpans([
      { start: "2026-09-02T09:00:00Z", end: "2026-09-02T09:03:00Z" },
      { start: "2026-09-02T08:00:00Z", end: "2026-09-02T08:01:00Z" },
    ]);

    expect(spans).toEqual([
      { start: "2026-09-02T09:00:00.000Z", end: "2026-09-02T09:03:00.000Z" },
    ]);
  });
});

describe("plotGeometry", () => {
  it("exposes the plot insets and the time-axis y for outside consumers", () => {
    expect(plotGeometry(1000)).toEqual({ left: 32, width: 936, axisY: 38 });
    expect(plotGeometry(0)).toEqual({ left: 32, width: 256, axisY: 38 });
  });
});

const contextMessages: NonNullable<RunTimelineResponse["messages"]> = [
  {
    key: "c.0",
    idx: 0,
    ts: null,
    kind: "prompt",
    source: null,
    chars: 400,
    parts: [{ kind: "prompt", chars: 400 }],
  },
  {
    key: "c.1",
    idx: 1,
    ts: "2026-09-02T08:05:00Z",
    kind: "ai",
    source: null,
    chars: 300,
    parts: [{ kind: "text", chars: 300 }],
  },
  {
    key: "c.2",
    idx: 2,
    ts: "2026-09-02T08:40:00Z",
    kind: "ai",
    source: null,
    chars: 300,
    parts: [{ kind: "text", chars: 300 }],
  },
];

const secondRow: RunTimelineResponse["rows"][number] = {
  ...row,
  turn: 2,
  start: "2026-09-02T08:30:00Z",
  end: "2026-09-02T08:50:00Z",
};

describe("context axis (P4-2b)", () => {
  const window = { from: "2026-09-02T08:00:00Z", to: "2026-09-02T09:00:00Z" };

  it("projects strip, blocks, and turns linearly in character units", () => {
    const layout = buildTimelineLayout({
      width: 1000,
      window,
      rows: [row, secondRow],
      events: [],
      messages: contextMessages,
      axis: "context",
      contextView: { from: 0, to: 1000 },
      layers: [
        { id: "L0#0", depth: 0, parent: null, start: window.from, end: window.to, summary: "overview" },
        { id: "L1#9", depth: 1, parent: "L0#0", start: window.from, end: "2026-09-02T08:01:00Z", summary: "empty stage" },
      ],
    });

    // Strip: exact cumulative char positions on the 936px plot.
    const strip = layout.strip;
    expect(strip).not.toBeNull();
    expect(strip?.messages[0].left).toBeCloseTo(32, 6);
    expect(strip?.messages[1].left).toBeCloseTo(32 + 400 * 0.936, 6);
    expect(strip?.messages[2].left).toBeCloseTo(32 + 700 * 0.936, 6);

    // Blocks: the covered-message extent; the ts-less head message is never
    // covered, and a node without covered messages is omitted.
    expect(layout.layerRows[0].blocks).toHaveLength(1);
    expect(layout.layerRows[0].blocks[0].left).toBeCloseTo(32 + 400 * 0.936, 6);
    expect(layout.layerRows[0].blocks[0].width).toBeCloseTo(600 * 0.936, 6);
    expect(layout.layerRows[1].blocks).toHaveLength(0);

    // Turns: the same covered-message mapping (M5).
    expect(layout.turns).toHaveLength(2);
    expect(layout.turns[0].left).toBeCloseTo(32 + 400 * 0.936, 6);
    expect(layout.turns[0].width).toBeCloseTo(300 * 0.936, 6);
    expect(layout.turns[1].left).toBeCloseTo(32 + 700 * 0.936, 6);
  });

  it("draws 1-2-5 character ticks across the viewport", () => {
    const layout = buildTimelineLayout({
      width: 1000,
      window,
      rows: [row],
      events: [],
      messages: contextMessages,
      axis: "context",
      contextView: { from: 0, to: 1000 },
    });

    expect(layout.ticks.map((tick) => tick.key)).toEqual(["0", "200", "400", "600", "800", "1000"]);
    expect(layout.ticks.map((tick) => tick.label)).toEqual(["0", "200", "400", "600", "800", "1.0k"]);
    expect(layout.ticks[0].x).toBe(32);
    expect(layout.ticks[1].x).toBe(219);
    expect(layout.ticks[5].x).toBe(968);
  });

  it("maps a zoomed viewport without clamping geometry (the clip cuts)", () => {
    const layout = buildTimelineLayout({
      width: 1000,
      window,
      rows: [row],
      events: [],
      messages: contextMessages,
      axis: "context",
      contextView: { from: 600, to: 800 },
      layers: [
        { id: "L0#0", depth: 0, parent: null, start: window.from, end: window.to, summary: "overview" },
      ],
    });

    const strip = layout.strip;
    expect(strip?.messages[2].left).toBeCloseTo(32 + 100 * 4.68, 6);
    expect(strip?.messages[2].width).toBeCloseTo(300 * 4.68, 6);
    // Raw geometry: the block's left edge lies outside the plot to the left.
    const block = layout.layerRows[0].blocks[0];
    expect(block.left).toBeCloseTo(32 + (400 - 600) * 4.68, 6);
    expect(block.left).toBeLessThan(layout.plot.left);
    expect(block.left + block.width).toBeCloseTo(32 + (1000 - 600) * 4.68, 6);
  });

  it("fails fast when the context axis has no viewport", () => {
    expect(() =>
      buildTimelineLayout({
        width: 1000,
        window,
        rows: [row],
        events: [],
        messages: contextMessages,
        axis: "context",
      }),
    ).toThrow(/contextView/);
  });
});
