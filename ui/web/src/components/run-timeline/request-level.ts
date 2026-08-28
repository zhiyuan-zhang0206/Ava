const SIX_HOURS_MS = 6 * 60 * 60 * 1000;

export interface TimelineWindowOverride {
  from: string;
  to: string;
}

/** Keep server-selected sessions and long explicit windows bounded before rendering. */
export function usesTimelineBuckets(window: TimelineWindowOverride | null): boolean {
  if (window === null) return true;
  return Date.parse(window.to) - Date.parse(window.from) >= SIX_HOURS_MS;
}
