import { describe, expect, it } from "vitest";

import type { RunTimelineMessageBar, RunTimelineNode, RunTimelineUnit } from "@/lib/contracts/types";

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
import { timeAxis } from "./timeline-model";
import { ADDED_ROW, INPUT_ROW, UNITS_ROW } from "./timeline-nav";

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
  const message = (idx: number, from: number, to: number, own: number, total: number): RunTimelineMessageBar => ({
    idx, start: iso(from), end: iso(to), session: 0, context_tokens: own, estimated: false, context_total: total, request: null,
  });
  const data = {
    nodes: [node("a", 0, 100)],
    units: [unit(0, 0, 50), unit(1, 50, 100)],
    messages: [message(0, 0, 50, 4, 10), message(1, 50, 100, 16, 30)],
  };
  const whole = { from: T, to: T + 100_000 };
  const axis = timeAxis(whole);

  it("keys items by what they select, and looks them up from the layout", () => {
    const layout = blockLayout(UNITS_ROW, data, axis, axis.viewU(whole), 1000);
    expect(layout.wide.map((p) => p.key)).toEqual(["utext-0-0", "utext-1-1"]);
    expect(layout.items.get("utext-1-1")?.selection).toEqual({ kind: "unit", i0: 1, i1: 1, unitKind: "text" });
    expect(selectionKey({ kind: "node", id: "a" })).toBe("na");
    expect(selectionKey({ kind: "message", idx: 4 })).toBe("m4");
  });

  it("puts a message's bars exactly under the block that shows it, in both context rows", () => {
    const view = axis.viewU(whole);
    const units = blockLayout(UNITS_ROW, data, axis, view, 1000);
    for (const row of [INPUT_ROW, ADDED_ROW]) {
      const bars = barLayout(row, data, axis, view, 1000);
      expect(bars.wide.map((p) => [p.x0, p.x1])).toEqual(units.wide.map((p) => [p.x0, p.x1]));
      // A bar's frame hugs the box it is drawn in.
      const bar = bars.wide.find((p) => p.key === "m1");
      expect(bars.boxes.get("m1")).toEqual({ x0: bar?.x0, x1: bar?.x1 });
    }
  });

  it("gives an instant message the minimum width at its time, in every row alike", () => {
    const instant = { ...data, units: [unit(0, 20, 20)], messages: [message(0, 20, 20, 1, 1)] };
    const view = axis.viewU(whole);
    for (const row of [UNITS_ROW, INPUT_ROW, ADDED_ROW]) {
      const layout = row === UNITS_ROW ? blockLayout(row, instant, axis, view, 1000) : barLayout(row, instant, axis, view, 1000);
      // 20 s of 100 s on 1000 px: starts at 200 and is MIN_ITEM_PX wide (a narrow item: one painted run of columns).
      const key = row === UNITS_ROW ? "utext-0-0" : "m0";
      expect(layout.boxes.get(key)).toEqual({ x0: 200, x1: 203 });
      expect(layout.cells).toEqual([{ x0: 200, x1: 203, key }]);
    }
  });

  it("lets neighbouring instants inside one pixel column share it: one painted item, the heavier bar", () => {
    const close = {
      ...data,
      units: [unit(0, 20, 20), unit(1, 20.1, 20.1)],
      messages: [message(0, 20, 20, 1, 5), message(1, 20.1, 20.1, 1, 50)],
    };
    const view = axis.viewU(whole);
    const units = blockLayout(UNITS_ROW, close, axis, view, 1000);
    // Both blocks are drawn from their own times, overlapping, and each column is painted once.
    expect(units.boxes.get("utext-1-1")?.x0).toBeCloseTo(201);
    const columns = units.cells.flatMap((c) => Array.from({ length: c.x1 - c.x0 }, (_, i) => c.x0 + i));
    expect(new Set(columns).size).toBe(columns.length);
    const bars = barLayout(INPUT_ROW, close, axis, view, 1000);
    expect(bars.cells.find((c) => c.x0 <= 201 && 201 < c.x1)?.key).toBe("m1");
  });

  it("lays bars out with their values: the context through the message, or its own weight by square root", () => {
    expect(barLayout(INPUT_ROW, data, axis, axis.viewU(whole), 1000).values?.get("m1")).toBe(30);
    expect(barLayout(ADDED_ROW, data, axis, axis.viewU(whole), 1000).values?.get("m1")).toBe(4);
    const crowded = barLayout(INPUT_ROW, data, axis, axis.viewU(whole), 3);
    expect(crowded.wide).toEqual([]);
    expect(crowded.cells.length).toBeGreaterThan(0);
  });
});

describe("frameOf", () => {
  it("hugs the union of the drawn boxes exactly", () => {
    expect(frameOf([{ x0: 10, x1: 14 }, { x0: 30, x1: 33 }], 1000)).toEqual({ left: 10, width: 23 });
    // A bar 3 px wide is framed at 3 px, never widened.
    expect(frameOf([{ x0: 100, x1: 103 }], 1000)).toEqual({ left: 100, width: 3 });
    expect(frameOf([], 1000)).toBeNull();
  });

  it("frames only the visible part of an item running past an edge, and nothing for one off the track", () => {
    // A bar from -26 to 141 is framed from 0 to 141, not shifted right to keep its full width.
    expect(frameOf([{ x0: -26, x1: 141 }], 860)).toEqual({ left: 0, width: 141 });
    expect(frameOf([{ x0: 800, x1: 900 }], 860)).toEqual({ left: 800, width: 60 });
    expect(frameOf([{ x0: -40, x1: -10 }], 860)).toBeNull();
    expect(frameOf([{ x0: 900, x1: 950 }], 860)).toBeNull();
  });
});
