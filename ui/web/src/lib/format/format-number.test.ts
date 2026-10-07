import { describe, expect, it } from "vitest";

import { formatTokens, formatTokensCompact } from "./format-number";

describe("formatTokens (breakdown precision)", () => {
  it("stays raw below 1k and scales k / M below a billion", () => {
    expect(formatTokens(940)).toBe("940");
    expect(formatTokens(26_300)).toBe("26.3k");
    expect(formatTokens(1_000_000)).toBe("1.00M");
  });

  it("carries to B at a billion with two decimals", () => {
    expect(formatTokens(4_109_300_000)).toBe("4.11B");
  });

  it("keeps values just below the B floor in M", () => {
    expect(formatTokens(999_900_000)).toBe("999.90M");
  });

  it("renders a mantissa rounded into the next unit under that unit", () => {
    expect(formatTokens(999_999_000)).toBe("1.00B");
  });

  it("carries trillions at two decimals", () => {
    expect(formatTokens(1_234_000_000_000)).toBe("1.23T");
  });
});

describe("formatTokensCompact (compact precision)", () => {
  it("stays raw below 1k and scales k / M below a billion", () => {
    expect(formatTokensCompact(820)).toBe("820");
    expect(formatTokensCompact(1_234)).toBe("1.2k");
    expect(formatTokensCompact(3_400_000)).toBe("3.4M");
  });

  it("carries to B at a billion with two decimals", () => {
    expect(formatTokensCompact(4_109_300_000)).toBe("4.11B");
  });

  it("keeps values just below the B floor in M", () => {
    expect(formatTokensCompact(999_900_000)).toBe("999.9M");
  });

  it("renders a mantissa rounded into the next unit under that unit", () => {
    expect(formatTokensCompact(999_999_000)).toBe("1.00B");
  });
});
