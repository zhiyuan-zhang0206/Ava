const SIX_HOURS_MS = 6 * 60 * 60 * 1000;

export interface TimelineWindowOverride {
  from: string;
  to: string;
}

/** Aggregate only long explicit windows before the turn count is known. */
export function usesTimelineBuckets(window: TimelineWindowOverride | null): boolean {
  if (window === null) return false;
  return Date.parse(window.to) - Date.parse(window.from) >= SIX_HOURS_MS;
}
