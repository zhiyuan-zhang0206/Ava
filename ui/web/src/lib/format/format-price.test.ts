import { describe, expect, it } from "vitest";
import { formatModelPrice } from "./format-price";

describe("model rate precision", () => {
  it.each([
    [0, "$0.00"],
    [0.003, "$0.003"],
    [0.003625, "$0.003625"],
    [0.014, "$0.014"],
    [0.036, "$0.036"],
    [0.000000001, "$0.000000001"],
    [0.8, "$0.80"],
    [8, "$8.00"],
  ])("formats %s as %s", (value, expected) => {
    expect(formatModelPrice(value)).toBe(expected);
  });
});
