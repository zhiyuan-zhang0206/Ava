import { describe, expect, it } from "vitest";

import { readScrollMemory, saveScrollMemory } from "./scroll-memory";

// The store is a module-level map, so each test uses its own entry key.
describe("scroll memory (per history entry)", () => {
  it("returns null for an unknown entry", () => {
    expect(readScrollMemory("_b_unknown_")).toBeNull();
  });

  it("round-trips a saved position and overwrites on the next save", () => {
    saveScrollMemory("_b_1_", { contentKey: "42", scrollTop: 700, followBottom: false });
    expect(readScrollMemory("_b_1_")).toEqual({
      contentKey: "42",
      scrollTop: 700,
      followBottom: false,
    });
    saveScrollMemory("_b_1_", { contentKey: "42", scrollTop: 900, followBottom: true });
    expect(readScrollMemory("_b_1_")?.scrollTop).toBe(900);
    expect(readScrollMemory("_b_1_")?.followBottom).toBe(true);
  });

  it("keeps entries isolated from each other", () => {
    saveScrollMemory("_b_2_", { contentKey: "7", scrollTop: 120, followBottom: false });
    expect(readScrollMemory("_b_3_")).toBeNull();
    expect(readScrollMemory("_b_2_")?.scrollTop).toBe(120);
  });
});
