/** Small, dependency-free scale for the linear timeline axis. */

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
