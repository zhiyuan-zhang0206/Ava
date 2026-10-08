import { describe, expect, it } from "vitest";

import type { RunTimelineNode, RunTimelineRequest, RunTimelineUnit } from "@/lib/contracts/types";

import {
  aggregateColumns,
  barLayout,
  blockLayout,
  buildHitIndex,
  frameOf,
  hitTest,
  selectionKey,
  snap,
  type Place,
} from "./timeline-canvas-model";
import { buildAxisMap } from "./timeline-model";
import { INPUT_ROW, UNITS_ROW } from "./timeline-nav";

const place = (key: string, x0: number, x1: number, weight = 0): Place => ({ key, x0, x1, weight });

describe("aggregateColumns", () => {
  it("keeps blocks at least the narrow width as blocks, and the rest as one painted item per pixel column", () => {
    const { wide, cells } = aggregateColumns([place("a", 10.1, 10.4), place("b", 10.5, 10.7), place("w", 50, 54), place("d", 30, 31.5)]);
    expect(wide.map((p) => p.key)).toEqual(["w"]);
    expect(cells.map((c) => [c.x0, c.x1])).toEqual([[10, 11], [30, 32]]);
    // The column holding a and b is painted once, for the one covering more of it.
    expect(cells[0].key).toBe("a");
  });

  it("lets the heavier item stand for a column, whatever covers more", () => {
    const { cells } = aggregateColumns([place("big", 7.0, 7.9, 1), place("tall", 7.9, 8.0, 5)]);
    expect(cells).toEqual([{ x0: 7, x1: 8, key: "tall" }]);
  });

  it("merges neighbouring columns of the same item into one run", () => {
    const { cells } = aggregateColumns([place("a", 5, 8), place("b", 8, 9)]);
    expect(cells).toEqual([{ x0: 5, x1: 8, key: "a" }, { x0: 8, x1: 9, key: "b" }]);
  });

  it("paints a column once however many items pile into it", () => {
    const many = aggregateColumns(Array.from({ length: 500 }, (_, i) => place(`k${i}`, 7 + i * 0.001, 7.0005 + i * 0.001)));
    expect(many.cells).toHaveLength(1);
    expect(many.cells[0]).toMatchObject({ x0: 7, x1: 8 });
  });

  it("gives an instant its column", () => {
    expect(aggregateColumns([place("m", 12.4, 12.4)]).cells).toEqual([{ x0: 12, x1: 13, key: "m" }]);
  });
});

describe("hitTest", () => {
  const index = buildHitIndex(
    aggregateColumns([place("a", 0, 40), place("b", 20, 60), place("c", 100, 140), place("hair", 200.2, 200.3), place("hair2", 210, 210.5)]),
  );

  it("answers with the block under the pointer, the later-starting one where blocks overlap", () => {
    expect(hitTest(index, 10)).toBe("a");
    expect(hitTest(index, 30)).toBe("b");
    expect(hitTest(index, 59)).toBe("b");
    expect(hitTest(index, 120)).toBe("c");
  });

  it("finds nothing in a gap, and reaches a hairline within a pixel", () => {
    expect(hitTest(index, 80)).toBeNull();
    expect(hitTest(index, 200.5)).toBe("hair");
    expect(hitTest(index, 201.4)).toBe("hair");
    expect(hitTest(index, 205)).toBeNull();
    expect(hitTest(index, 209.5)).toBe("hair2");
    expect(hitTest(index, -5)).toBeNull();
  });

  it("is a binary search: an index of thousands answers correctly", () => {
    const places = Array.from({ length: 5000 }, (_, i) => place(`p${i}`, i * 10, i * 10 + 8));
    const big = buildHitIndex(aggregateColumns(places, 4));
    expect(hitTest(big, 24_903)).toBe("p2490");
    expect(hitTest(big, 24_909)).toBeNull();
  });
});

describe("snap", () => {
  it("lands on the device pixel grid", () => {
    expect(snap(10.3, 1)).toBe(10);
    expect(snap(10.3, 2)).toBe(10.5);
    expect(snap(10.26, 2)).toBe(10.5);
  });
});

describe("row layouts", () => {
  const T = Date.parse("2026-10-04T12:00:00Z");
  const iso = (sec: number) => new Date(T + sec * 1000).toISOString();
  const unit = (i0: number, from: number, to: number): RunTimelineUnit => ({
    kind: "text", i0, i1: i0, start: iso(from), end: iso(to), source: null, preview: "", parent: null,
    context_tokens: null, generation_tokens: null, estimated: null,
  });
  const node = (id: string, from: number, to: number): RunTimelineNode => ({
    id, level: 1, parent: null, start: iso(from), end: iso(to), span_start: 0, span_end: 1, summary: id,
    usage: { calls: 0, input: 0, cache_read: 0, output: 0, cache_write: 0, cost_usd: 0, cost_calls: 0 }, generation: null, context_tokens: null, estimated: null,
  });
  const request = (idx: number, tokens: number, from: number): RunTimelineRequest => ({
    idx, ts: iso(idx), session: 0, input_tokens: tokens, output_tokens: 1, added_tokens: 1, added_estimated: false, added_from: from, added_to: idx,
  });
  const data = { nodes: [node("a", 0, 100)], units: [unit(0, 0, 50), unit(1, 50, 100)], requests: [request(1, 10, 0), request(2, 30, 1)] };
  const whole = { from: T, to: T + 100_000 };
  const axis = buildAxisMap(data.units, whole, "time");

  it("keys items by what they select, and looks them up from the layout", () => {
    const layout = blockLayout(UNITS_ROW, data, axis, axis.viewU(whole), 1000);
    expect(layout.wide.map((p) => p.key)).toEqual(["utext-0-0", "utext-1-1"]);
    expect(layout.items.get("utext-1-1")?.selection).toEqual({ kind: "unit", i0: 1, i1: 1, unitKind: "text" });
    expect(selectionKey({ kind: "node", id: "a" })).toBe("na");
    // A bar's frame hugs the box it is drawn in (the span less its gaps), not the message range it covers.
    const bars = barLayout(INPUT_ROW, data, axis, axis.viewU(whole), 1000);
    const bar = bars.wide.find((p) => p.key === "r1");
    expect(bars.boxes.get("r1")).toEqual({ x0: bar?.x0, x1: bar?.x1 });
    expect(selectionKey({ kind: "request", idx: 4 })).toBe("r4");
  });

  it("lays bars out with their values, the taller standing for a crowded column", () => {
    const layout = barLayout(INPUT_ROW, data, axis, axis.viewU(whole), 1000);
    expect(layout.values?.get("r2")).toBe(30);
    const crowded = barLayout(INPUT_ROW, data, axis, axis.viewU(whole), 3);
    expect(crowded.wide).toEqual([]);
    expect(crowded.cells.length).toBeGreaterThan(0);
  });
});

describe("frameOf", () => {
  it("hugs the union of the drawn boxes exactly when no minimum is asked for", () => {
    expect(frameOf([{ x0: 10, x1: 14 }, { x0: 30, x1: 33 }], 0, 1000)).toEqual({ left: 10, width: 23 });
    // A bar 3 px wide is framed at 3 px, not widened to the 6 px minimum of the other rows.
    expect(frameOf([{ x0: 100, x1: 103 }], 0, 1000)).toEqual({ left: 100, width: 3 });
  });

  it("widens a thin item to the minimum around its middle and keeps the frame on the track", () => {
    expect(frameOf([{ x0: 100, x1: 100.5 }], 6, 1000)).toEqual({ left: 97.25, width: 6 });
    expect(frameOf([{ x0: 0, x1: 1 }], 6, 1000)?.left).toBe(0);
    expect(frameOf([{ x0: 999, x1: 1000 }], 6, 1000)).toEqual({ left: 994, width: 6 });
    expect(frameOf([], 6, 1000)).toBeNull();
  });

  it("frames only the visible part of an item running past an edge, and nothing for one off the track", () => {
    // A bar from -26 to 141 is framed from 0 to 141, not shifted right to keep its full width.
    expect(frameOf([{ x0: -26, x1: 141 }], 0, 860)).toEqual({ left: 0, width: 141 });
    expect(frameOf([{ x0: 800, x1: 900 }], 0, 860)).toEqual({ left: 800, width: 60 });
    expect(frameOf([{ x0: -40, x1: -10 }], 0, 860)).toBeNull();
    expect(frameOf([{ x0: 900, x1: 950 }], 0, 860)).toBeNull();
  });
});
