import { describe, expect, it } from "vitest";

import { usesTimelineBuckets } from "./request-level";

describe("usesTimelineBuckets", () => {
  it("uses a bounded overview for a server-selected session and windows of six hours or more", () => {
    expect(usesTimelineBuckets(null)).toBe(true);
    expect(usesTimelineBuckets({ from: "2026-08-29T08:00:00Z", to: "2026-08-29T14:00:00Z" })).toBe(true);
  });

  it("keeps narrower user-selected windows at turn detail", () => {
    expect(usesTimelineBuckets({ from: "2026-08-29T08:00:00Z", to: "2026-08-29T13:59:59Z" })).toBe(false);
  });
});
