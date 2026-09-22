import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

// The console ships Ava's brand favicon at Next's /favicon.ico convention
// (user order 2026-09-22, task #4445). Lock it at the file level: the icon
// state has flipped twice through unrelated PRs, and leaving it to
// convention files alone is exactly how it slips.
describe("favicon", () => {
  it("ships a real ICO asset at Next's /favicon.ico convention", () => {
    const bytes = readFileSync("src/app/favicon.ico");

    expect([...bytes.subarray(0, 4)]).toEqual([0, 0, 1, 0]);
    expect(bytes.length).toBeGreaterThan(1_000);
  });
});
