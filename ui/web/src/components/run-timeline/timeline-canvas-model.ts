// Pure model of the canvas rows: where each item of a row is drawn in pixels, how items narrower
// than a few pixels collapse to one drawn column each, and which item a pointer position hits.
// No React, no canvas, no I/O.

import {
  barBox,
  layoutSpans,
  maxAdded,
  maxInput,
  NARROW_DRAW_PX,
  type AxisMap,
  type Selection,
  type Viewport,
} from "./timeline-model";
import { ADDED_ROW, INPUT_ROW, navItems, navRowIds, type NavData, type NavItem } from "./timeline-nav";

/** One item of a row on the track, in pixels from its left edge. */
export interface Place {
  key: string;
  x0: number;
  x1: number;
  /** What decides which of several narrow items stands for their pixel column (the larger wins; a tie goes to the one covering more of the column). */
  weight: number;
}

/** A run of whole pixel columns painted for one item: the item that stands for each of them. */
export interface Cell {
  x0: number;
  x1: number;
  key: string;
}

export interface RowLayout {
  /** Items wide enough to be drawn as blocks, in x order. */
  wide: Place[];
  /** The narrow items, collapsed to one painted item per pixel column and run-merged. */
  cells: Cell[];
  items: ReadonlyMap<string, NavItem>;
  /** A bar row's value per item (what its height is), absent for other rows. */
  values?: ReadonlyMap<string, number>;
}

/** The identity of an item across redraws. */
export function selectionKey(selection: Selection): string {
  if (selection.kind === "node") return `n${selection.id}`;
  if (selection.kind === "request") return `r${selection.idx}`;
  return `u${selection.unitKind}-${selection.i0}-${selection.i1}`;
}

/**
 * Splits places into blocks (at least `narrowPx` wide) and one painted item per pixel column for
 * the rest: of the narrow items touching a column, the heaviest wins, then the one covering most
 * of it. Every column is painted at most once, so piling up items never darkens or thickens it.
 */
export function aggregateColumns(
  places: readonly Place[],
  narrowPx: number = NARROW_DRAW_PX,
): { wide: Place[]; cells: Cell[] } {
  const wide: Place[] = [];
  const best = new Map<number, { key: string; weight: number; cover: number }>();
  for (const place of places) {
    if (place.x1 - place.x0 >= narrowPx) {
      wide.push(place);
      continue;
    }
    const first = Math.floor(place.x0);
    const last = Math.max(Math.ceil(place.x1), first + 1);
    for (let column = first; column < last; column += 1) {
      const cover = Math.max(Math.min(place.x1, column + 1) - Math.max(place.x0, column), 1e-6);
      const held = best.get(column);
      if (held === undefined || place.weight > held.weight || (place.weight === held.weight && cover > held.cover)) {
        best.set(column, { key: place.key, weight: place.weight, cover });
      }
    }
  }
  const cells: Cell[] = [];
  for (const column of [...best.keys()].sort((a, b) => a - b)) {
    const { key } = best.get(column) as { key: string };
    const tail = cells.at(-1);
    if (tail?.x1 === column && tail.key === key) tail.x1 = column + 1;
    else cells.push({ x0: column, x1: column + 1, key });
  }
  return { wide: wide.sort((a, b) => a.x0 - b.x0 || a.x1 - b.x1), cells };
}

/** Largest end among `wide[0..i]`, so a search can stop looking back. */
function runningEnds(wide: readonly Place[]): number[] {
  const ends: number[] = [];
  for (const place of wide) ends.push(Math.max(place.x1, ends.at(-1) ?? -Infinity));
  return ends;
}

/** A row's layout made searchable: items sorted by x, with the running maximum of their ends. */
export interface HitIndex {
  wide: readonly Place[];
  ends: readonly number[];
  cells: readonly Cell[];
}

export function buildHitIndex(layout: Pick<RowLayout, "wide" | "cells">): HitIndex {
  return { wide: layout.wide, ends: runningEnds(layout.wide), cells: layout.cells };
}

/** The last index in `sorted` whose `start` is at most `x`, or -1. */
function lastAtOrBefore(count: number, startAt: (i: number) => number, x: number): number {
  let lo = 0;
  let hi = count - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (startAt(mid) <= x) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

/**
 * The key of the item under pixel `x` of a row, or null. A painted column (a narrow item) wins over
 * the block under it, since it is drawn on top; of overlapping blocks the later-starting one wins; a
 * column also answers for the `tolerance` pixels beside it, so a hairline stays reachable.
 */
export function hitTest(index: HitIndex, x: number, tolerance = 1): string | null {
  // The hairlines are drawn over the blocks, so they answer first.
  const at = lastAtOrBefore(index.cells.length, (k) => index.cells[k].x0, x);
  const here = at >= 0 ? index.cells[at] : undefined;
  if (here !== undefined && x < here.x1) return here.key;
  for (let i = lastAtOrBefore(index.wide.length, (k) => index.wide[k].x0, x); i >= 0 && index.ends[i] >= x; i -= 1) {
    if (index.wide[i].x1 >= x) return index.wide[i].key;
  }
  const near = [here, index.cells[at + 1]]
    .filter((cell): cell is Cell => cell !== undefined)
    .map((cell) => ({ cell, gap: x < cell.x0 ? cell.x0 - x : x - cell.x1 }))
    .filter(({ gap }) => gap <= tolerance)
    .sort((a, b) => a.gap - b.gap)
    .at(0);
  return near === undefined ? null : near.cell.key;
}

/** Snaps a CSS-pixel coordinate to the device pixel grid, so a fill has no half-covered edge. */
export function snap(x: number, dpr: number): number {
  return Math.round(x * dpr) / dpr;
}

type Placer = Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">;

function layoutOf(
  places: readonly Place[],
  items: readonly NavItem[],
  values?: ReadonlyMap<string, number>,
): RowLayout {
  const { wide, cells } = aggregateColumns(places);
  return { wide, cells, items: new Map(items.map((item) => [selectionKey(item.selection), item])), values };
}

/** The layout of a level or Messages row: blocks placed on the axis as the DOM rows were (a body never reaches the next block's start). */
export function blockLayout(row: string, data: NavData, axis: Placer, viewU: Viewport, trackPx: number): RowLayout {
  const items = navItems(row, data, axis);
  const keyed = items.map((item) => ({ key: selectionKey(item.selection), u0: item.u0, u1: item.u1 }));
  const places = layoutSpans(keyed, viewU, trackPx).map((place) => ({
    key: place.key,
    x0: place.left,
    x1: place.left + place.width,
    weight: 0,
  }));
  return layoutOf(places, items);
}

/** What a request bar's height is, and the largest of it over the requests (what the row scales to). */
export function barValue(row: string, request: { input_tokens: number; added_tokens: number }): number {
  return row === ADDED_ROW ? Math.sqrt(request.added_tokens) : request.input_tokens;
}

export function barTop(row: string, data: NavData): number {
  return row === ADDED_ROW ? Math.sqrt(maxAdded(data.requests)) : maxInput(data.requests);
}

/** The layout of a context row: one bar per request, the tallest standing for a crowded column. */
export function barLayout(row: string, data: NavData, axis: Placer, viewU: Viewport, trackPx: number): RowLayout {
  const items = navItems(row, data, axis).filter((item) => item.request !== undefined);
  const values = new Map<string, number>();
  const places: Place[] = [];
  for (const item of items) {
    const box = barBox({ u0: item.u0, u1: item.u1 }, viewU, trackPx);
    if (box.left + box.width < 0 || box.left > trackPx || item.request === undefined) continue;
    const key = selectionKey(item.selection);
    const value = barValue(row, item.request);
    values.set(key, value);
    places.push({ key, x0: box.left, x1: box.left + box.width, weight: value });
  }
  return layoutOf(places, items, values);
}

/** The layout of any row of the page by its id. */
export function rowLayout(row: string, data: NavData, axis: Placer, viewU: Viewport, trackPx: number): RowLayout {
  return row === INPUT_ROW || row === ADDED_ROW
    ? barLayout(row, data, axis, viewU, trackPx)
    : blockLayout(row, data, axis, viewU, trackPx);
}

let cache: { data: NavData; axis: Placer; from: number; to: number; trackPx: number; layouts: Map<string, RowLayout> } | null = null;

/**
 * The layout of every row for one view, remembered until the data, axis, view or track width changes:
 * hovering, selecting and highlighting repaint over it without laying anything out again.
 */
export function layoutsFor(data: NavData, axis: Placer, view: Viewport, trackPx: number): Map<string, RowLayout> {
  const hit = cache;
  if (hit !== null && hit.data === data && hit.axis === axis && hit.from === view.from && hit.to === view.to && hit.trackPx === trackPx) {
    return hit.layouts;
  }
  const layouts = new Map(navRowIds(data).map((row) => [row, rowLayout(row, data, axis, view, trackPx)]));
  cache = { data, axis, from: view.from, to: view.to, trackPx, layouts };
  return layouts;
}
