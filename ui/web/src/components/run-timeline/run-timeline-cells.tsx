"use client";

// Drawing-only layers of the run timeline rows: the merged fill of blocks too narrow to draw one by
// one, and the overlay that keeps the selection visible at any zoom.

import { cn } from "@/lib/format/utils";

import type { DrawCell } from "./timeline-model";

/** How one merged cell is painted: its fill and opacity class. */
export interface CellPaint {
  background: string;
  className?: string;
}

/** The fill of the narrow blocks of a row: one box per `DrawCell`, never darker for the blocks piled in it. */
export function NarrowCells({
  cells,
  paint,
  inset = "inset-y-0",
}: {
  cells: readonly DrawCell[];
  paint: (keys: readonly string[]) => CellPaint;
  /** The vertical extent of the cell in its row. */
  inset?: string;
}) {
  return cells.map((cell) => {
    const { background, className } = paint(cell.keys);
    return (
      <span
        key={`${cell.left}-${cell.keys[0]}`}
        aria-hidden="true"
        data-testid="run-timeline-cell"
        data-count={cell.keys.length}
        className={cn("pointer-events-none absolute", inset, className)}
        style={{ left: cell.left, width: cell.width, background }}
      />
    );
  });
}

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
