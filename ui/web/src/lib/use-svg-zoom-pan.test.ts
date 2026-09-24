import { describe, expect, it } from "vitest";

import { fitSvgBoxTransform } from "@/lib/use-svg-zoom-pan";

describe("fitSvgBoxTransform", () => {
  it("centers a padded focus box within an offset viewBox", () => {
    const transform = fitSvgBoxTransform(
      { minX: 20, minY: 0, maxX: 40, maxY: 10 },
      { minX: -100, minY: -50, w: 200, h: 100 },
      5,
      1,
    );
    expect(transform.k).toBe(5);
    expect(transform.x).toBe(-150);
    expect(transform.y).toBe(-25);
  });

  it("applies the fit ratio without changing the focus center", () => {
    const transform = fitSvgBoxTransform(
      { minX: 20, minY: 0, maxX: 40, maxY: 10 },
      { minX: -100, minY: -50, w: 200, h: 100 },
      5,
      0.5,
    );
    expect(transform.k).toBe(2.5);
    expect(transform.x).toBe(-75);
    expect(transform.y).toBe(-12.5);
  });

  it("keeps a positive floor when fitting a very large box", () => {
    const transform = fitSvgBoxTransform(
      { minX: -5000, minY: -5000, maxX: 5000, maxY: 5000 },
      { minX: 0, minY: 0, w: 1, h: 1 },
      0,
      1,
    );
    expect(transform.k).toBe(0.001);
    expect(transform.x).toBe(0.5);
    expect(transform.y).toBe(0.5);
  });
});
