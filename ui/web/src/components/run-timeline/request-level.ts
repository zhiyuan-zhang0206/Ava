const SIX_HOURS_MS = 6 * 60 * 60 * 1000;
const MIN_BAR_PX = 12;
const CANVAS_BUDGET = 1000;
const MIN_WINDOW_MS = 60_000;
const RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
const MAX_BUCKETS = 96;
const BUCKETS = [
  { seconds: 60, label: "1m" },
  { seconds: 300, label: "5m" },
  { seconds: 600, label: "10m" },
  { seconds: 1800, label: "30m" },
  { seconds: 3600, label: "1h" },
  { seconds: 10_800, label: "3h" },
  { seconds: 21_600, label: "6h" },
  { seconds: 43_200, label: "12h" },
  { seconds: 86_400, label: "1d" },
] as const;

export interface TimelineWindowOverride {
  from: string;
  to: string;
}

/** Aggregate only long explicit windows before the turn count is known. */
export function usesTimelineBuckets(window: TimelineWindowOverride | null): boolean {
  if (window === null) return false;
  return Date.parse(window.to) - Date.parse(window.from) >= SIX_HOURS_MS;
}

/** Aggregate when one readable bar per turn cannot fit on the chart canvas. */
export function needsBuckets(nTurns: number): boolean {
  return nTurns * MIN_BAR_PX > CANVAS_BUDGET;
}

/** Choose the narrowest bucket size that keeps the overview bounded. */
export function pickBucketSeconds(spanMs: number, _nTurns: number | null): number {
  const boundedSpanMs = Math.max(spanMs, MIN_WINDOW_MS);
  return (
    BUCKETS.find(({ seconds }) => Math.ceil(boundedSpanMs / (seconds * 1000)) <= MAX_BUCKETS)
      ?.seconds ?? BUCKETS[BUCKETS.length - 1].seconds
  );
}

export function bucketLabel(sizeSeconds: number): string {
  const bucket = BUCKETS.find(({ seconds }) => seconds === sizeSeconds);
  if (!bucket) throw new RangeError(`Unsupported timeline bucket size: ${sizeSeconds}`);
  return bucket.label;
}

/** Resize a timeline window while preserving the timestamp beneath its anchor. */
export function zoomWindowAround(
  window: TimelineWindowOverride,
  factor: number,
  anchor: number,
  now: Date,
): TimelineWindowOverride {
  const from = Date.parse(window.from);
  const to = Date.parse(window.to);
  const nowMs = now.getTime();
  if (!(to > from) || !Number.isFinite(nowMs)) {
    throw new RangeError("Timeline zoom requires a valid increasing window and current time");
  }
  if (!Number.isFinite(factor) || factor <= 0) {
    throw new RangeError("Timeline zoom factor must be positive");
  }
  if (!Number.isFinite(anchor) || anchor < 0 || anchor > 1) {
    throw new RangeError("Timeline zoom anchor must be between zero and one");
  }

  const currentSpanMs = to - from;
  const nextSpanMs = Math.min(RETENTION_MS, Math.max(MIN_WINDOW_MS, currentSpanMs * factor));
  const anchorTimestamp = from + currentSpanMs * anchor;
  let nextFrom = anchorTimestamp - nextSpanMs * anchor;
  let nextTo = nextFrom + nextSpanMs;
  if (nextTo > nowMs) {
    nextFrom -= nextTo - nowMs;
    nextTo = nowMs;
  }
  const retentionStart = nowMs - RETENTION_MS;
  if (nextFrom < retentionStart) {
    nextTo += retentionStart - nextFrom;
    nextFrom = retentionStart;
  }

  return { from: new Date(nextFrom).toISOString(), to: new Date(nextTo).toISOString() };
}

export function centerZoomWindow(
  window: TimelineWindowOverride,
  factor: number,
  now: Date,
): TimelineWindowOverride {
  return zoomWindowAround(window, factor, 0.5, now);
}
