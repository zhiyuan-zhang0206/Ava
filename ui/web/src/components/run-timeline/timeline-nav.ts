// Keyboard navigation and the selection overlay of the run timeline: pure functions over the rows'
// items on the shared x axis. No React, no I/O.

import type { RunTimelineNode, RunTimelineRequest, RunTimelineUnit } from "@/lib/contracts/types";

import {
  isSelected,
  levelsTopFirst,
  requestCovers,
  requestSpan,
  requestUnits,
  type AxisMap,
  type AxisSpan,
  type Selection,
  type Viewport,
} from "./timeline-model";

/** What keyboard navigation moves over. */
export interface NavData {
  nodes: readonly RunTimelineNode[];
  units: readonly RunTimelineUnit[];
  requests: readonly RunTimelineRequest[];
}

export type NavKey = "left" | "right" | "up" | "down";

/**
 * One selectable thing of a row: a node, a block, or a request (its bar). Every row is a list of
 * these, each with its extent on the shared x axis (`u0`..`u1`, axis coordinates) and in time.
 */
export interface NavItem {
  row: string;
  selection: Selection;
  u0: number;
  u1: number;
  /** Its extent in epoch milliseconds (a request spans the blocks it read, else is one instant). */
  start: number;
  end: number;
  node?: RunTimelineNode;
  unit?: RunTimelineUnit;
  request?: RunTimelineRequest;
}

export const UNITS_ROW = "units";
export const INPUT_ROW = "input";
export const ADDED_ROW = "added";
export const levelRowId = (level: number) => `level-${level}`;

/** The rows top to bottom, as the page draws them. */
export function navRowIds(data: NavData): string[] {
  const rows = levelsTopFirst(data.nodes).map(levelRowId);
  rows.push(UNITS_ROW);
  if (data.requests.length > 0) rows.push(INPUT_ROW, ADDED_ROW);
  return rows;
}

/** The items of one row, left to right on the axis. */
export function navItems(row: string, data: NavData, axis: Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">): NavItem[] {
  let items: NavItem[];
  if (row === UNITS_ROW) {
    items = data.units.map((unit) => ({
      row,
      unit,
      selection: { kind: "unit", i0: unit.i0, i1: unit.i1, unitKind: unit.kind },
      ...axis.unitSpan(unit),
      start: Date.parse(unit.start),
      end: Date.parse(unit.end),
    }));
  } else if (row === INPUT_ROW || row === ADDED_ROW) {
    items = data.requests.map((request) => {
      const covered = requestUnits(request, data.units);
      const sent = Date.parse(request.ts);
      return {
        row,
        request,
        selection: { kind: "request", idx: request.idx },
        ...requestSpan(request, data.units, axis),
        start: covered.length > 0 ? Math.min(...covered.map((unit) => Date.parse(unit.start))) : sent,
        end: covered.length > 0 ? Math.max(...covered.map((unit) => Date.parse(unit.end))) : sent,
      };
    });
  } else {
    items = data.nodes
      .filter((node) => levelRowId(node.level) === row)
      .map((node) => ({
        row,
        node,
        selection: { kind: "node", id: node.id },
        ...axis.nodeSpan(node),
        start: Date.parse(node.start),
        end: Date.parse(node.end),
      }));
  }
  return items.sort((a, b) => a.u0 - b.u0 || a.u1 - b.u1 || a.start - b.start);
}

function indexOfSelection(items: readonly NavItem[], selection: Selection): number {
  return items.findIndex((item) => isSelected(selection, item.selection));
}

/** Whether `parent` is the direct container of `child`: a node over its child nodes and blocks, a block over the requests that read it, a request over itself in the other context row. */
function isParentOf(parent: NavItem, child: NavItem): boolean {
  if (parent.node !== undefined) return child.node?.parent === parent.node.id || child.unit?.parent === parent.node.id;
  if (parent.unit !== undefined) return child.request !== undefined && requestCovers(child.request, parent.unit);
  return parent.request !== undefined && parent.request.idx === child.request?.idx;
}

/** How far `target` overlaps `here` on the x axis; apart, minus the gap between them (so the nearest scores highest). */
function overlapScore(here: NavItem, target: NavItem): number {
  return Math.min(here.u1, target.u1) - Math.max(here.u0, target.u0);
}

/** The target item to move vertically to: a related one (parent or child) when the row has any, else the one overlapping most on the x axis, else the nearest. */
function verticalTarget(here: NavItem, targets: readonly NavItem[]): NavItem | undefined {
  const related = targets.find((item) => isParentOf(here, item) || isParentOf(item, here));
  if (related !== undefined) return related;
  return targets.reduce<NavItem | undefined>((best, item) => {
    if (best === undefined) return item;
    const gain = overlapScore(here, item) - overlapScore(here, best);
    if (gain !== 0) return gain > 0 ? item : best;
    // Equal: the one whose middle is nearer.
    const mid = (here.u0 + here.u1) / 2;
    return Math.abs((item.u0 + item.u1) / 2 - mid) < Math.abs((best.u0 + best.u1) / 2 - mid) ? item : best;
  }, undefined);
}

/** The row a selection lives in when no row is remembered for it. */
function rowOfSelection(selection: Selection, data: NavData): string | null {
  if (selection.kind === "unit") return UNITS_ROW;
  if (selection.kind === "request") return INPUT_ROW;
  const node = data.nodes.find((candidate) => candidate.id === selection.id);
  return node === undefined ? null : levelRowId(node.level);
}

/**
 * The next selection of an arrow key. Every row (Level N..1, Messages, Context size, Added context)
 * is a list of items with an x extent on the axis; `row` is the row the current selection was made in
 * (the two context rows select the same request, so the row cannot be read off it).
 *
 * left / right: the previous / next item of the row (none past either end).
 * up / down: the adjacent row's item that is the current one's parent or child (a node's parent or
 *   first child, a block's level-1 node, the request that read a block, a request's blocks, the same
 *   request in the other context row); with no such relation, the item overlapping the current one
 *   most on the x axis, else the nearest.
 * No current selection: the leftmost item in the viewport (Messages first), else the row's first.
 * Returns null when there is nowhere to go.
 */
export function navigate(
  key: NavKey,
  current: { row: string | null; selection: Selection } | null,
  data: NavData,
  axis: Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">,
  view: Viewport,
): { row: string; item: NavItem } | null {
  const rows = navRowIds(data);
  if (current === null) {
    const order = [UNITS_ROW, ...rows.filter((row) => row !== UNITS_ROW).reverse()];
    for (const row of order) {
      const inView = navItems(row, data, axis).find((item) => item.end >= view.from && item.start <= view.to);
      if (inView !== undefined) return { row, item: inView };
    }
    return null;
  }
  let row = current.row !== null && rows.includes(current.row) ? current.row : null;
  let items = row === null ? [] : navItems(row, data, axis);
  let at = row === null ? -1 : indexOfSelection(items, current.selection);
  if (at < 0) {
    row = rowOfSelection(current.selection, data);
    items = row === null ? [] : navItems(row, data, axis);
    at = indexOfSelection(items, current.selection);
  }
  if (row === null || at < 0) return navigate(key, null, data, axis, view);
  if (key === "left" || key === "right") {
    const to = at + (key === "left" ? -1 : 1);
    const next = to < 0 ? undefined : items.at(to);
    return next === undefined ? null : { row, item: next };
  }
  const to = rows.indexOf(row) + (key === "up" ? -1 : 1);
  const targetRow = to < 0 ? undefined : rows.at(to);
  if (targetRow === undefined) return null;
  const found = verticalTarget(items[at], navItems(targetRow, data, axis));
  return found === undefined ? null : { row: targetRow, item: found };
}

/** The narrowest a selection's overlay box is drawn, in pixels, so it stays visible at any zoom. */
export const SELECTION_MIN_PX = 6;

/**
 * Where the selected items sit in each row: the item itself, and for a request also the blocks it
 * read and its bar in both context rows. Rows with nothing selected are absent.
 */
export function selectionSpans(
  selection: Selection | null,
  data: NavData,
  axis: Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">,
): Map<string, AxisSpan[]> {
  const out = new Map<string, AxisSpan[]>();
  if (selection === null) return out;
  const request = selection.kind === "request" ? data.requests.find((candidate) => candidate.idx === selection.idx) : undefined;
  for (const row of navRowIds(data)) {
    const lit = navItems(row, data, axis).filter(
      (item) =>
        isSelected(selection, item.selection) ||
        (request !== undefined && item.unit !== undefined && requestCovers(request, item.unit)),
    );
    if (lit.length > 0) out.set(row, lit.map(({ u0, u1 }) => ({ u0, u1 })));
  }
  return out;
}

/** The span that covers every given span; null for none. */
export function spansExtent(spans: readonly AxisSpan[]): AxisSpan | null {
  if (spans.length === 0) return null;
  return { u0: Math.min(...spans.map((span) => span.u0)), u1: Math.max(...spans.map((span) => span.u1)) };
}

/** A span's overlay box in pixels on a track `trackPx` wide showing `viewU`: at least `minPx` wide, centred on the span, kept inside the track; null when it lies outside the view. */
export function overlayBox(
  span: AxisSpan,
  viewU: Viewport,
  trackPx: number,
  minPx: number = SELECTION_MIN_PX,
): { left: number; width: number } | null {
  if (!(viewU.to > viewU.from) || span.u1 < viewU.from || span.u0 > viewU.to) return null;
  const scale = trackPx / (viewU.to - viewU.from);
  const left = (span.u0 - viewU.from) * scale;
  const right = (span.u1 - viewU.from) * scale;
  const width = Math.min(Math.max(right - left, minPx), trackPx);
  const centred = (left + right) / 2 - width / 2;
  return { left: Math.min(Math.max(centred, 0), trackPx - width), width };
}

/** The viewport that shows [startMs, endMs] when `view` does not: the same width, centred on it, inside the base extent; `view` itself when it already shows some of it. */
export function revealView(axis: AxisMap, view: Viewport, base: Viewport, startMs: number, endMs: number): Viewport {
  const u0 = axis.toU(startMs, "lo");
  const u1 = axis.toU(endMs, "hi");
  const shown = axis.viewU(view);
  if (u1 >= shown.from && u0 <= shown.to) return view;
  const width = shown.to - shown.from;
  const baseU = axis.viewU(base);
  const from = Math.min(Math.max((u0 + u1) / 2 - width / 2, baseU.from), baseU.to - width);
  return axis.viewFromU({ from, to: from + width });
}
