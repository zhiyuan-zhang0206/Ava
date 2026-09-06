import { describe, expect, it } from "vitest";

import {
  GLYPH_WIDTH_FULL,
  LABEL_LINE_HEIGHT,
  linkStrengthForWeight,
  wrapLabel,
} from "./force-graph";

describe("linkStrengthForWeight", () => {
  it("maps edge weight monotonically into the configured strength", () => {
    expect(linkStrengthForWeight(0, 100, 0.4)).toBeCloseTo(0.1);
    expect(linkStrengthForWeight(25, 100, 0.4)).toBeCloseTo(0.175);
    expect(linkStrengthForWeight(100, 100, 0.4)).toBeCloseTo(0.4);
  });

  it("clamps malformed or out-of-range weights", () => {
    expect(linkStrengthForWeight(-1, 100, 0.4)).toBeCloseTo(0.1);
    expect(linkStrengthForWeight(200, 100, 0.4)).toBeCloseTo(0.4);
    expect(() => linkStrengthForWeight(1, 0, 0.4)).toThrow(
      "Maximum edge weight must be positive",
    );
  });
});

describe("wrapLabel", () => {
  it("returns one label-only line for a short label", () => {
    expect(wrapLabel("alpha", 30)).toEqual(["alpha"]);
    expect(wrapLabel("alpha", 30)).not.toContain("#alpha");
  });

  it("wraps a realistic long label without an ellipsis", () => {
    const label = "a".repeat(30);
    const lines = wrapLabel(label, 30);

    expect(lines.length).toBeGreaterThan(1);
    expect(lines.join("")).toBe(label);
    expect(lines.every((line) => !line.includes("…"))).toBe(true);
  });

  it("breaks at spaces before slicing a word", () => {
    expect(wrapLabel("alpha beta gamma", 22)).toEqual(["alpha beta", "gamma"]);
  });

  it("breaks after a hyphen before slicing a word", () => {
    expect(wrapLabel("alpha-beta", 18)).toEqual(["alpha-", "beta"]);
  });

  it("never exceeds the horizontal character budget", () => {
    const radius = 30;
    const maxChars = Math.floor((2 * radius - 8) / 3.6);

    expect(wrapLabel("b".repeat(30), radius).every((line) => line.length <= maxChars)).toBe(true);
  });

  it("clamps an extreme label to the vertical budget and ellipsizes its last line", () => {
    const radius = 14;
    const maxChars = Math.floor((2 * radius - 8) / 3.6);
    const maxLabelLines = Math.floor((2 * radius - 4) / LABEL_LINE_HEIGHT) - 1;
    const lines = wrapLabel("c".repeat(60), radius);

    expect(lines).toHaveLength(maxLabelLines);
    expect(lines.every((line) => line.length <= maxChars)).toBe(true);
    expect(lines.at(-1)?.endsWith("…")).toBe(true);
  });

  it("wraps CJK labels at the full glyph width so they stay inside the node", () => {
    const radius = 20;
    const maxLineWidth = 2 * radius - 8;
    const label = "\u4e2d\u6587\u6807\u7b7e\u6ea2\u51fa".repeat(2); // 12 full-width chars
    const lines = wrapLabel(label, radius);

    // No chars lost (the 12-char label fits the vertical budget at 5/line).
    expect(lines.join("")).toBe(label);
    // Every line fits the node's horizontal budget measured at the real CJK
    // advance (6px) — the old 3.6px/char estimate packed 8 chars into a line
    // that only holds 5 (QA #651: Chinese titles bled ~20px past the node).
    expect(lines.every((line) => line.length * GLYPH_WIDTH_FULL <= maxLineWidth)).toBe(true);
    expect(Math.max(...lines.map((l) => l.length))).toBe(Math.floor(maxLineWidth / GLYPH_WIDTH_FULL));
  });

  it("mixes narrow and full-width glyphs on one line by measured width", () => {
    // "abc" = 3 × 3.6 = 10.8px + "\u4e2d\u6587" = 2 × 6 = 12px = 22.8px fits the
    // 24px budget (r=16); one more Latin char would overflow it.
    expect(wrapLabel("abc\u4e2d\u6587def", 16)).toEqual(["abc\u4e2d\u6587", "def"]);
  });

  it("breaks before an embedded Latin word instead of splitting it (QA #651 follow-up)", () => {
    // "\u8d44\u6e90\u76d1\u63a7\uff08" = 5 × 6 = 30px leaves only 6px on the 36px line (r=22) —
    // just "C" of "Cluster" would fit, so the word must move to its own line
    // whole instead of rendering "\u8d44\u6e90\u76d1\u63a7\uff08C" / "luster Ops".
    expect(wrapLabel("\u8d44\u6e90\u76d1\u63a7\uff08Cluster Ops \u57df\uff09", 22)).toEqual([
      "\u8d44\u6e90\u76d1\u63a7\uff08",
      "Cluster",
      "Ops \u57df\uff09",
    ]);
    expect(wrapLabel("\u5f71\u5206\u8eab\uff08Think like a master\uff09", 22)).toEqual([
      "\u5f71\u5206\u8eab\uff08",
      "Think like",
      "a master\uff09",
    ]);
  });

  it("still splits a Latin word when it is wider than the line", () => {
    // QA rule: char-break only when the word itself exceeds the line width.
    // An over-wide word after a CJK prefix moves to its own line whole, then
    // is hard-cut by the budget; a pure-Latin label keeps the same behavior.
    expect(wrapLabel("\u8d44\u6e90\u76d1\u63a7\uff08Supercalifragilistic \u57df\uff09", 22)).toEqual([
      "\u8d44\u6e90\u76d1\u63a7\uff08",
      "Supercalif",
      "ragilistic",
      "\u57df\uff09",
    ]);
    expect(wrapLabel("supercalifragilistic", 22)).toEqual(["supercalif", "ragilistic"]);
  });
});
