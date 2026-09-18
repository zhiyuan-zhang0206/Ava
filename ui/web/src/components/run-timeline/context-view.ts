// Local char-domain viewport for the run timeline's context axis (P4-2b,
// task #4023). The time axis IS the fetch window, so its pan/zoom changes the
// request; the context axis is a pure projection of the already-fetched
// messages — its viewport lives here as plain number math in character units,
// and gestures over it never refetch. Pure so the clamping contract is
// directly testable.

export interface TimelineContextView {
  from: number;
  to: number;
}

// KEEP (task #3696 exception inventory): zoom floor — a two-message context
// cannot collapse into an unusable sliver (the demo's clampView floor for
// the context axis). Interaction detail, not a user setting.
export const MIN_CONTEXT_SPAN_CHARS = 30;

/** Clamp a context viewport into [0, total], enforcing the minimum span. */
export function clampContextView(from: number, to: number, total: number): TimelineContextView {
  if (!Number.isFinite(from) || !Number.isFinite(to) || !Number.isFinite(total) || total < 0) {
    throw new RangeError("Context view requires finite positions and a non-negative total");
  }
  const bounded = Math.max(0, total);
  const minSpan = Math.min(MIN_CONTEXT_SPAN_CHARS, bounded);
  const maxSpan = Math.max(bounded, minSpan);
  const span = Math.min(Math.max(to - from, minSpan), maxSpan);
  let start = Math.min(Math.max(from, 0), bounded);
  if (start + span > bounded) start = bounded - span;
  if (start < 0) start = 0;
  return { from: start, to: start + span };
}

/** Slide a context viewport by a fraction of its own span (wheel / drag pan).
 *  Clamped by clampContextView, so a pan stops at either domain edge. */
export function panContextView(
  view: TimelineContextView,
  deltaFraction: number,
  total: number,
): TimelineContextView {
  if (!Number.isFinite(deltaFraction)) {
    throw new RangeError("Context pan offset must be a finite fraction");
  }
  const span = view.to - view.from;
  const shift = span * deltaFraction;
  return clampContextView(view.from + shift, view.to + shift, total);
}

/** Resize a context viewport while preserving the character beneath its
 *  anchor (0 = left edge, 1 = right edge), clamped to the domain. */
export function zoomContextViewAround(
  view: TimelineContextView,
  factor: number,
  anchor: number,
  total: number,
): TimelineContextView {
  if (!Number.isFinite(factor) || factor <= 0) {
    throw new RangeError("Context zoom factor must be positive");
  }
  if (!Number.isFinite(anchor) || anchor < 0 || anchor > 1) {
    throw new RangeError("Context zoom anchor must be between zero and one");
  }
  const bounded = Math.max(0, total);
  const span = view.to - view.from;
  const minSpan = Math.min(MIN_CONTEXT_SPAN_CHARS, bounded);
  const maxSpan = Math.max(bounded, minSpan);
  const nextSpan = Math.min(Math.max(span * factor, minSpan), maxSpan);
  const anchorChar = view.from + span * anchor;
  const nextFrom = anchorChar - nextSpan * anchor;
  return clampContextView(nextFrom, nextFrom + nextSpan, total);
}
