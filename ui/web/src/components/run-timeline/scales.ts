/** Small, dependency-free scales for the two independently measured panels. */

export function tokenBarWidth(tokens: number, maximumTokens: number, plotWidth: number): number {
  if (tokens <= 0 || maximumTokens <= 0 || plotWidth <= 0) return 0;
  return Math.min(plotWidth, (tokens / maximumTokens) * plotWidth);
}

export function timeCoordinate(
  timestamp: string,
  windowStart: string,
  windowEnd: string,
  plotWidth: number,
): number {
  const start = Date.parse(windowStart);
  const end = Date.parse(windowEnd);
  const value = Date.parse(timestamp);
  if (!Number.isFinite(start) || !Number.isFinite(end) || !Number.isFinite(value) || end <= start) {
    return 0;
  }
  return Math.max(0, Math.min(plotWidth, ((value - start) / (end - start)) * plotWidth));
}
