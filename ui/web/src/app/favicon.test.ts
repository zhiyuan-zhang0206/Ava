import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

describe("favicon", () => {
  it("ships a real ICO asset at Next's /favicon.ico convention", () => {
    const bytes = readFileSync("src/app/favicon.ico");

    expect([...bytes.subarray(0, 4)]).toEqual([0, 0, 1, 0]);
    expect(bytes.length).toBeGreaterThan(1_000);
  });
});
