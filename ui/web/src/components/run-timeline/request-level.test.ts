import { describe, expect, it } from "vitest";

import {
  bucketLabel,
  centerZoomWindow,
  needsBuckets,
  pickBucketSeconds,
  usesTimelineBuckets,
  zoomWindowAround,
} from "./request-level";

describe("usesTimelineBuckets", () => {
  it("loads server-selected sessions as turns before deciding whether to aggregate", () => {
    expect(usesTimelineBuckets(null)).toBe(false);
  });

  it("uses a bounded overview for explicit windows of six hours or more", () => {
    expect(usesTimelineBuckets({ from: "2026-08-29T08:00:00Z", to: "2026-08-29T14:00:00Z" })).toBe(true);
  });

  it("keeps narrower user-selected windows at turn detail", () => {
    expect(usesTimelineBuckets({ from: "2026-08-29T08:00:00Z", to: "2026-08-29T13:59:59Z" })).toBe(false);
  });
});

describe("needsBuckets", () => {
  it("aggregates as soon as minimum readable bars exceed the canvas budget", () => {
    expect(needsBuckets(83)).toBe(false);
    expect(needsBuckets(84)).toBe(true);
    expect(needsBuckets(111)).toBe(true);
  });
});

describe("pickBucketSeconds", () => {
  it.each([
    [60_000, 60],
    [96 * 60_000, 60],
    [96 * 60_000 + 1, 300],
    [8 * 60 * 60_000, 300],
    [96 * 24 * 60 * 60_000 + 1, 86_400],
  ])("bounds a %i ms span with %i-second buckets", (spanMs, expected) => {
    expect(pickBucketSeconds(spanMs, 111)).toBe(expected);
    expect(pickBucketSeconds(spanMs, null)).toBe(expected);
  });

  it("rounds sub-minute spans up to one minute", () => {
    expect(pickBucketSeconds(1, 1)).toBe(60);
  });
});

describe("bucketLabel", () => {
  it.each([
    [60, "1m"],
    [300, "5m"],
    [600, "10m"],
    [1800, "30m"],
    [3600, "1h"],
    [10_800, "3h"],
    [21_600, "6h"],
    [43_200, "12h"],
    [86_400, "1d"],
  ])("labels %i seconds as %s", (seconds, expected) => {
    expect(bucketLabel(seconds)).toBe(expected);
  });
});

describe("zoomWindowAround", () => {
  const now = new Date("2026-08-29T12:00:00Z");

  it("keeps the timestamp under the cursor fixed", () => {
    expect(
      zoomWindowAround(
        { from: "2026-08-29T08:00:00Z", to: "2026-08-29T10:00:00Z" },
        0.5,
        0.25,
        now,
      ),
    ).toEqual({
      from: "2026-08-29T08:15:00.000Z",
      to: "2026-08-29T09:15:00.000Z",
    });
  });

  it("clamps zoomed windows to one minute, now, and seven-day retention", () => {
    expect(
      centerZoomWindow(
        { from: "2026-08-29T10:00:00Z", to: "2026-08-29T11:00:00Z" },
        0.001,
        now,
      ),
    ).toEqual({
      from: "2026-08-29T10:29:30.000Z",
      to: "2026-08-29T10:30:30.000Z",
    });
    expect(
      centerZoomWindow(
        { from: "2026-08-29T10:00:00Z", to: "2026-08-29T12:00:00Z" },
        2,
        now,
      ),
    ).toEqual({
      from: "2026-08-29T08:00:00.000Z",
      to: "2026-08-29T12:00:00.000Z",
    });
    expect(
      centerZoomWindow(
        { from: "2026-08-28T10:00:00Z", to: "2026-08-29T10:00:00Z" },
        10,
        now,
      ),
    ).toEqual({
      from: "2026-08-22T12:00:00.000Z",
      to: "2026-08-29T12:00:00.000Z",
    });
  });
});
