// Timeline content-column width helpers — clamp + CSS max-width construction
// for the display.timeline_width_ratio setting (Task #715).

import { describe, expect, it } from "vitest";

import {
  clampTimelineRatio,
  TIMELINE_NARROW_BREAKPOINT_PX,
  TIMELINE_WIDTH_RATIO_DEFAULT,
  TIMELINE_WIDTH_RATIO_MAX,
  TIMELINE_WIDTH_RATIO_MIN,
  TIMELINE_MAX_CAP_PX,
  timelineMaxWidthCss,
} from "./timeline-width";

// Task #805: below this width the timeline is full-width and the ratio is
// ignored. Locked to Tailwind's md breakpoint — the same 768px boundary the
// sidebar uses for its mobile overlay drawer.
describe("TIMELINE_NARROW_BREAKPOINT_PX", () => {
  it("is the md breakpoint (768px)", () => {
    expect(TIMELINE_NARROW_BREAKPOINT_PX).toBe(768);
  });
});

describe("clampTimelineRatio", () => {
  it("keeps in-range values", () => {
    expect(clampTimelineRatio(0.4)).toBe(0.4);
    expect(clampTimelineRatio(TIMELINE_WIDTH_RATIO_MIN)).toBe(TIMELINE_WIDTH_RATIO_MIN);
    expect(clampTimelineRatio(TIMELINE_WIDTH_RATIO_MAX)).toBe(TIMELINE_WIDTH_RATIO_MAX);
  });

  it("clamps out-of-range values", () => {
    expect(clampTimelineRatio(0.05)).toBe(TIMELINE_WIDTH_RATIO_MIN);
    expect(clampTimelineRatio(2)).toBe(TIMELINE_WIDTH_RATIO_MAX);
  });

  it("falls back to the default for non-numbers / corrupt values", () => {
    expect(clampTimelineRatio(undefined)).toBe(TIMELINE_WIDTH_RATIO_DEFAULT);
    expect(clampTimelineRatio(null)).toBe(TIMELINE_WIDTH_RATIO_DEFAULT);
    expect(clampTimelineRatio("0.4")).toBe(TIMELINE_WIDTH_RATIO_DEFAULT);
    expect(clampTimelineRatio(Number.NaN)).toBe(TIMELINE_WIDTH_RATIO_DEFAULT);
  });
});

describe("timelineMaxWidthCss", () => {
  it("renders <ratio>vw against the cap for the default ratio", () => {
    expect(timelineMaxWidthCss(0.4)).toBe(`min(40vw, ${TIMELINE_MAX_CAP_PX}px)`);
  });

  it("scales the viewport fraction with the ratio", () => {
    expect(timelineMaxWidthCss(0.6)).toBe(`min(60vw, ${TIMELINE_MAX_CAP_PX}px)`);
  });

  it("keeps the cap as the ceiling at high ratios", () => {
    expect(timelineMaxWidthCss(0.8)).toBe(`min(80vw, ${TIMELINE_MAX_CAP_PX}px)`);
  });

  it("falls back to the default ratio for a missing value", () => {
    expect(timelineMaxWidthCss(undefined)).toBe(`min(40vw, ${TIMELINE_MAX_CAP_PX}px)`);
  });
});
