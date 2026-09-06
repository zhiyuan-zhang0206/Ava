// LAYOUT_INVARIANTS integrity — the shared checklist between the jsdom
// class-contract layer (page.test.tsx, fleet tests) and the real-engine
// Playwright layer (tests/e2e/test_layout_invariants.py). A broken list
// (duplicate id, unknown page, wrong tier set) must fail here, fast, in the
// ms-level jsdom suite — not 40 minutes into an e2e run.
import { describe, expect, it } from "vitest";

import {
  BAR_CLEAR_TOP_PADDING_CLASS,
  BAR_DIVIDER_CLASS,
  BAR_HEIGHT_CLASS,
  BAR_HEIGHT_PX,
  LAYOUT_INVARIANTS,
  LAYOUT_INVARIANT_IDS,
  LAYOUT_VIEWPORT_TIERS,
} from "./layout";

describe("home title bars", () => {
  it("uses the compact 44px bar with 8px of timeline clearance", () => {
    expect(BAR_HEIGHT_PX).toBe(44);
    expect(BAR_HEIGHT_CLASS).toBe("h-11");
    expect(BAR_CLEAR_TOP_PADDING_CLASS).toBe("pt-[52px]");
  });

  it("insets each horizontal divider so adjacent panel segments do not join", () => {
    expect(BAR_DIVIDER_CLASS).toContain("after:inset-x-1");
    expect(BAR_DIVIDER_CLASS).toContain("after:h-px");
  });
});

describe("LAYOUT_INVARIANTS", () => {
  it("ids are the canonical I1–I6 sequence, unique", () => {
    expect(LAYOUT_INVARIANT_IDS).toEqual(["I1", "I2", "I3", "I4", "I5", "I6"]);
    expect(new Set(LAYOUT_INVARIANT_IDS).size).toBe(LAYOUT_INVARIANT_IDS.length);
  });

  it("every invariant names a valid page set", () => {
    const pages = new Set(["timeline", "fleet"]);
    for (const inv of LAYOUT_INVARIANTS) {
      expect(inv.name.length).toBeGreaterThan(0);
      expect(inv.assertion.length).toBeGreaterThan(0);
      expect(inv.pages.length).toBeGreaterThan(0);
      for (const p of inv.pages) expect(pages.has(p)).toBe(true);
    }
  });

  it("the three viewport tiers are exactly 320 / 390 / 768", () => {
    expect([...LAYOUT_VIEWPORT_TIERS]).toEqual([320, 390, 768]);
  });

});
