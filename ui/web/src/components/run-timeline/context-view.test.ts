import { describe, expect, it } from "vitest";

import {
  clampContextView,
  MIN_CONTEXT_SPAN_CHARS,
  panContextView,
  zoomContextViewAround,
} from "./context-view";

describe("clampContextView", () => {
  it("keeps a valid viewport untouched", () => {
    expect(clampContextView(120, 240, 1000)).toEqual({ from: 120, to: 240 });
  });

  it("clamps both edges into the domain", () => {
    expect(clampContextView(-80, 40, 1000)).toEqual({ from: 0, to: 120 });
    expect(clampContextView(980, 1100, 1000)).toEqual({ from: 880, to: 1000 });
  });

  it("enforces the minimum span", () => {
    const clamped = clampContextView(500, 505, 1000);
    expect(clamped.to - clamped.from).toBe(MIN_CONTEXT_SPAN_CHARS);
  });

  it("caps the span at the total when the total is below the floor", () => {
    expect(clampContextView(3, 5, 10)).toEqual({ from: 0, to: 10 });
    expect(clampContextView(0, 40, 10)).toEqual({ from: 0, to: 10 });
  });

  it("collapses to zero width for an empty domain", () => {
    expect(clampContextView(0, 0, 0)).toEqual({ from: 0, to: 0 });
    expect(clampContextView(10, 50, 0)).toEqual({ from: 0, to: 0 });
  });

  it("rejects non-finite input and negative totals", () => {
    expect(() => clampContextView(Number.NaN, 10, 100)).toThrow(RangeError);
    expect(() => clampContextView(0, 10, -1)).toThrow(RangeError);
  });
});

describe("panContextView", () => {
  it("slides by a fraction of the span", () => {
    expect(panContextView({ from: 100, to: 200 }, 0.5, 1000)).toEqual({ from: 150, to: 250 });
  });

  it("stops at both domain edges", () => {
    expect(panContextView({ from: 0, to: 100 }, -0.5, 1000)).toEqual({ from: 0, to: 100 });
    expect(panContextView({ from: 900, to: 1000 }, 0.5, 1000)).toEqual({ from: 900, to: 1000 });
  });
});

describe("zoomContextViewAround", () => {
  it("zooms out about the anchor (full view stays full)", () => {
    expect(zoomContextViewAround({ from: 400, to: 600 }, 2, 0.5, 1000)).toEqual({
      from: 300,
      to: 700,
    });
    expect(zoomContextViewAround({ from: 0, to: 1000 }, 1.6, 0.5, 1000)).toEqual({
      from: 0,
      to: 1000,
    });
  });

  it("zooms in about the anchor and respects the minimum span", () => {
    expect(zoomContextViewAround({ from: 0, to: 1000 }, 0.1, 0.5, 1000)).toEqual({
      from: 450,
      to: 550,
    });
    const floored = zoomContextViewAround({ from: 0, to: 40 }, 0.1, 0.5, 1000);
    expect(floored.to - floored.from).toBe(MIN_CONTEXT_SPAN_CHARS);
  });

  it("keeps the anchored character fixed where possible", () => {
    const before = { from: 200, to: 400 };
    const anchor = 0.25; // char 250
    const after = zoomContextViewAround(before, 2, anchor, 1000);
    expect(after.from + (after.to - after.from) * anchor).toBeCloseTo(250);
  });

  it("rejects invalid factors and anchors", () => {
    expect(() => zoomContextViewAround({ from: 0, to: 10 }, 0, 0.5, 100)).toThrow(RangeError);
    expect(() => zoomContextViewAround({ from: 0, to: 10 }, 1.5, 1.2, 100)).toThrow(RangeError);
  });
});
