import { describe, expect, it } from "vitest";

import { GLYPH_WIDTH_FULL, LABEL_LINE_HEIGHT, wrapLabel } from "./force-graph";

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
    const label = "中文标签溢出".repeat(2); // 12 full-width chars
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
    // "abc" = 3 × 3.6 = 10.8px + "中文" = 2 × 6 = 12px = 22.8px fits the
    // 24px budget (r=16); one more Latin char would overflow it.
    expect(wrapLabel("abc中文def", 16)).toEqual(["abc中文", "def"]);
  });
});
