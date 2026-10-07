/** USD per million tokens: keep cents for ordinary rates and small rates visible. */
export function formatModelPrice(value: number): string {
  const amount = value > 0 && value < 1
    ? value.toLocaleString("en-US", { maximumSignificantDigits: 4, useGrouping: false })
    : value.toFixed(2);
  const [whole, fraction = ""] = amount.split(".");
  return `$${whole}.${fraction.padEnd(2, "0")}`;
}
