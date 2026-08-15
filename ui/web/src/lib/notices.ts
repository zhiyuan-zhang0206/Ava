// Shared helpers for the "waiting on you" queue (the open notices that need a
// response — ava.ui.notify(require_response=True)), so the queue, the fleet tree
// badge, and the sidebar tree badge can't drift apart on priority ranking or
// colors.

import type { OpenNotice } from "./types";

// P0 highest .. P3 lowest -- the queue's primary sort key.
export const PRIORITY_RANK: Record<OpenNotice["priority"], number> = {
  P0: 0,
  P1: 1,
  P2: 2,
  P3: 3,
};

// Badge background by priority (the stakes axis). Callers add their own text
// color (white reads on all four; this theme has no destructive-foreground token).
export const PRIORITY_BG: Record<OpenNotice["priority"], string> = {
  P0: "bg-destructive",
  P1: "bg-amber-500",
  P2: "bg-sky-600",
  P3: "bg-slate-500",
};

// Text-color twin of PRIORITY_BG -- for SVG badge fills (fill: currentColor)
// where a bg-* utility cannot apply. Same hues, same stakes axis.
export const PRIORITY_TEXT: Record<OpenNotice["priority"], string> = {
  P0: "text-destructive",
  P1: "text-amber-500",
  P2: "text-sky-600",
  P3: "text-slate-500",
};

// The highest priority among an agent's open notices awaiting a response (P0
// wins), or null when it has none -- drives the per-node tree badge color.
export function topNoticePriority(
  notices: readonly OpenNotice[],
): OpenNotice["priority"] | null {
  for (const p of ["P0", "P1", "P2", "P3"] as const) {
    if (notices.some((n) => n.priority === p)) return p;
  }
  return null;
}
