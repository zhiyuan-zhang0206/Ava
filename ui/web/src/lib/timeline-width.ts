// Timeline content-column max width — a DB-backed user setting
// (display.timeline_width_ratio, a fraction of the viewport, default 0.6).
//
// Task #714 capped the column at a fixed max-w-3xl (768px); Task #715 makes
// the cap user-adjustable: the stored value is a RATIO of the viewport
// (per user request), rendered as `max-width: min(<ratio>vw, CAPpx)` so the
// column grows with the window but never exceeds a readable ceiling.
// Task #805: the ratio is a DESKTOP concept — on narrow viewports (phones,
// below TIMELINE_NARROW_BREAKPOINT_PX) the timeline and composer are
// full-width and the ratio is ignored entirely (see page.tsx).
//
// CAP: 1280px = 2/3 of a 1920px screen. Beyond that the centered-gutter
// design stops making sense (the column would swallow the window) and long
// text lines hurt readability (Claude/Gemini both cap around 768px; this
// ceiling still allows ~1.67x their comfort width). On a 1920px screen the
// slider's 0.2–0.8 range maps to 384–1280px; ratios above 0.667 are capped.

export const TIMELINE_WIDTH_RATIO_DEFAULT = 0.6;
export const TIMELINE_WIDTH_RATIO_MIN = 0.2;
export const TIMELINE_WIDTH_RATIO_MAX = 0.8;
export const TIMELINE_MAX_CAP_PX = 1280;

// Viewport width below which the timeline goes full-width and the ratio
// setting is ignored. Matches Tailwind's `md` breakpoint (768px), the same
// boundary the sidebar uses to switch between the fixed desktop rail and the
// mobile overlay drawer (components/agent-sidebar/).
export const TIMELINE_NARROW_BREAKPOINT_PX = 768;

/** Clamp a stored ratio into [MIN, MAX]; non-numbers fall back to the default
 *  (a corrupt/foreign value must never break the layout). */
export function clampTimelineRatio(value: unknown): number {
  const n = typeof value === "number" && Number.isFinite(value) ? value : TIMELINE_WIDTH_RATIO_DEFAULT;
  return Math.min(TIMELINE_WIDTH_RATIO_MAX, Math.max(TIMELINE_WIDTH_RATIO_MIN, n));
}

/** CSS max-width for the timeline content column: `<ratio>` of the viewport,
 *  capped at TIMELINE_MAX_CAP_PX. Desktop only — callers on narrow viewports
 *  (below TIMELINE_NARROW_BREAKPOINT_PX) must not apply this at all and let
 *  the column fill the window (full-width mobile timeline, task #805). */
export function timelineMaxWidthCss(ratio: unknown): string {
  return `min(${clampTimelineRatio(ratio) * 100}vw, ${TIMELINE_MAX_CAP_PX}px)`;
}
