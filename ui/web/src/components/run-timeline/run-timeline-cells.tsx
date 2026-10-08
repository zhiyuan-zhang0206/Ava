"use client";

// The selection's overlay on the run timeline rows, kept as a few DOM elements above the canvases so it stays visible at any zoom.

/** The selected item in a row: a clearly outlined box over everything, at least `SELECTION_MIN_PX` wide. */
export function SelectionBox({ box }: { box: { left: number; width: number } }) {
  return (
    <span
      aria-hidden="true"
      data-testid="run-timeline-selection-box"
      className="pointer-events-none absolute inset-y-0 z-10 rounded-[2px] border-2 border-foreground bg-foreground/10"
      style={{ left: box.left, width: box.width }}
    />
  );
}

/** The thin vertical line through every row at the selection's x position (its whole extent when several items are selected). */
export function SelectionLine({ box }: { box: { left: number; width: number } }) {
  return (
    <span
      aria-hidden="true"
      data-testid="run-timeline-selection-line"
      className="pointer-events-none absolute inset-y-0 z-10 border-x border-foreground/50"
      style={{ left: box.left, width: Math.max(box.width, 1) }}
    />
  );
}
