"use client";

// Resolved timeline colors for the current user settings — one hook for the
// whole view. `resolveTimelineColors` is memoized by the signature of the
// values it reads, so this returns the same object while no
// `display.color.*` setting changes — the timeline's per-row config memo
// relies on that stability (settings objects are replaced on ANY setting
// write, e.g. dragging the timeline-width slider).

import { resolveTimelineColors, type TimelineColors } from "./timeline-colors";
import { useUserSettings } from "./use-user-settings";

export function useTimelineColors(): TimelineColors {
  const { settings } = useUserSettings();
  return resolveTimelineColors(settings);
}
