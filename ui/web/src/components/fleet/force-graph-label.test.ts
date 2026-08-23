import { describe, expect, it } from "vitest";

import { LABEL_LINE_HEIGHT, wrapLabel } from "./force-graph";

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
});
