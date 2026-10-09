// Keyboard navigation and the selection overlay of the run timeline: pure functions over the rows'
// items on the shared x axis. No React, no I/O.

import type { RunTimelineNode, RunTimelineUnit } from "@/lib/contracts/types";

import {
  chainIds,
  isSelected,
  levelsTopFirst,
  contextUnits,
  type AxisMap,
  type Selection,
  type Viewport,
} from "./timeline-model";

/** What keyboard navigation moves over. */
export interface NavData {
  nodes: readonly RunTimelineNode[];
  units: readonly RunTimelineUnit[];
}

export type NavKey = "left" | "right" | "up" | "down";

/**
 * One selectable thing of a row: a node or a block (in the Messages row and, as a bar, in the Context size row). Every row is a list of
 * these, each with its extent on the shared x axis (`u0`..`u1`, axis coordinates) and in time.
 */
export interface NavItem {
  row: string;
  selection: Selection;
  u0: number;
  u1: number;
  /** Its extent in epoch milliseconds. */
  start: number;
  end: number;
  node?: RunTimelineNode;
  unit?: RunTimelineUnit;
}

export const UNITS_ROW = "units";
export const INPUT_ROW = "input";
export const levelRowId = (level: number) => `level-${level}`;

/** What the page's settings keep of an agent's rows. */
export interface RowOptions {
  /** How many understanding-tree levels are drawn, counted from the topmost; null draws them all. */
  levels: number | null;
  /** Whether the Context size row is drawn. */
  contextSize: boolean;
}

export const ALL_ROWS: RowOptions = { levels: null, contextSize: true };

/** The rows top to bottom, as the page draws them. */
export function navRowIds(data: NavData, options: RowOptions = ALL_ROWS): string[] {
  const levels = levelsTopFirst(data.nodes);
  const rows = (options.levels === null ? levels : levels.slice(0, options.levels)).map(levelRowId);
  rows.push(UNITS_ROW);
  if (options.contextSize && contextUnits(data.units).length > 0) rows.push(INPUT_ROW);
  return rows;
}

const itemCache = new WeakMap<NavData, WeakMap<object, Map<string, NavItem[]>>>();

/** The items of one row, left to right on the axis (remembered per data and axis: a render, a hover or a keypress asks again and again). */
export function navItems(row: string, data: NavData, axis: Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">): NavItem[] {
  let byAxis = itemCache.get(data);
  if (byAxis === undefined) {
    byAxis = new WeakMap();
    itemCache.set(data, byAxis);
  }
  let rows = byAxis.get(axis);
  if (rows === undefined) {
    rows = new Map();
    byAxis.set(axis, rows);
  }
  let items = rows.get(row);
  if (items === undefined) {
    items = buildItems(row, data, axis);
    rows.set(row, items);
  }
  return items;
}

function buildItems(row: string, data: NavData, axis: Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">): NavItem[] {
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
  } else if (row === INPUT_ROW) {
    items = contextUnits(data.units).map((unit) => ({
      row,
      unit,
      selection: { kind: "unit", i0: unit.i0, i1: unit.i1, unitKind: unit.kind },
      ...axis.unitSpan(unit),
      start: Date.parse(unit.start),
      end: Date.parse(unit.end),
    }));
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

/** Whether `parent` is the direct container of `child`: a node over its child nodes and blocks, the Messages row's block over its own bar in the Context size row. */
function isParentOf(parent: NavItem, child: NavItem): boolean {
  if (parent.node !== undefined) return child.node?.parent === parent.node.id || child.unit?.parent === parent.node.id;
  return parent.row === UNITS_ROW && child.row === INPUT_ROW && parent.unit === child.unit;
}

/** How far `target` overlaps `here` on the x axis; apart, minus the gap between them (so the nearest scores highest). */
function overlapScore(here: NavItem, target: NavItem): number {
  return Math.min(here.u1, target.u1) - Math.max(here.u0, target.u0);
}

/** The target item to move vertically to: a related one (parent or child, unless `related` is off: another agent's rows share no ids) when the row has any, else the one overlapping most on the x axis, else the nearest. */
export function verticalTarget(here: NavItem, targets: readonly NavItem[], related = true): NavItem | undefined {
  const kin = related ? targets.find((item) => isParentOf(here, item) || isParentOf(item, here)) : undefined;
  if (kin !== undefined) return kin;
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
  const node = data.nodes.find((candidate) => candidate.id === selection.id);
  return node === undefined ? null : levelRowId(node.level);
}

/**
 * The next selection of an arrow key. Every row (Level N..1, Messages, Context size)
 * is a list of items with an x extent on the axis; `row` is the row the current selection was made in
 * (a block sits in the Messages row and in the Context size row, so the row cannot be read off it).
 *
 * left / right: the previous / next item of the row (none past either end).
 * up / down: the adjacent row's item that is the current one's parent or child (a node's parent or
 *   first child, a block's level-1 node, a block's bar in the Context size row and back); with no
 *   such relation, the item overlapping the current one
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
  rows: readonly string[] = navRowIds(data),
): { row: string; item: NavItem } | null {
  if (current === null) return firstInView(data, axis, view, rows);
  const here = locate(current, data, axis, rows);
  if (here === null) return firstInView(data, axis, view, rows);
  const { row, items, at } = here;
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

/** The leftmost item in the viewport (Messages first, then the rows from the bottom up), else the first of a row. */
export function firstInView(
  data: NavData,
  axis: Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">,
  view: Viewport,
  rows: readonly string[],
): { row: string; item: NavItem } | null {
  const order = [UNITS_ROW, ...rows.filter((row) => row !== UNITS_ROW).reverse()].filter((row) => rows.includes(row));
  for (const row of order) {
    const inView = navItems(row, data, axis).find((item) => item.end >= view.from && item.start <= view.to);
    if (inView !== undefined) return { row, item: inView };
  }
  return null;
}

/** The row, items and position of the current selection among `rows`; null when it is not in any of them. */
export function locate(
  current: { row: string | null; selection: Selection },
  data: NavData,
  axis: Pick<AxisMap, "toU" | "unitSpan" | "nodeSpan">,
  rows: readonly string[],
): { row: string; items: NavItem[]; at: number } | null {
  let row = current.row !== null && rows.includes(current.row) ? current.row : null;
  let items = row === null ? [] : navItems(row, data, axis);
  let at = row === null ? -1 : indexOfSelection(items, current.selection);
  if (at < 0) {
    row = rowOfSelection(current.selection, data);
    items = row === null || !rows.includes(row) ? [] : navItems(row, data, axis);
    at = indexOfSelection(items, current.selection);
  }
  return row === null || at < 0 ? null : { row, items, at };
}

/** A primary item drawn narrower than this also gets a faint line through every row, so it can be found. */
export const SELECTION_LINE_BELOW_PX = 6;

/** The identity of an item across redraws. */
export function selectionKey(selection: Selection): string {
  if (selection.kind === "node") return `n${selection.id}`;
  return `u${selection.unitKind}-${selection.i0}-${selection.i1}`;
}

/** What the selection lights: the one primary item (the row the cursor is in), and the items linked to it, per row. */
export interface SelectionRoles {
  primary: { row: string; key: string } | null;
  linked: ReadonlyMap<string, ReadonlySet<string>>;
}

/**
 * The primary item is the selection in the row it was made in. Its links go one hop: a node links to
 * its ancestors; a block to its ancestors and its own bar in the other row (Messages or Context
 * size). Nothing links back down from a linked node: a node's ancestors never light the other
 * blocks they cover.
 */
export function selectionRoles(
  current: { row: string | null; selection: Selection } | null,
  data: NavData,
  rows: readonly string[] = navRowIds(data),
): SelectionRoles {
  const linked = new Map<string, Set<string>>();
  if (current === null) return { primary: null, linked };
  const { selection } = current;
  // Only a block can sit in either of two rows (Messages, Context size); a node is in the row of its level.
  const row =
    selection.kind === "unit" && current.row === INPUT_ROW && rows.includes(INPUT_ROW)
      ? INPUT_ROW
      : rowOfSelection(selection, data);
  const add = (target: string, key: string) => {
    let keys = linked.get(target);
    if (keys === undefined) {
      keys = new Set();
      linked.set(target, keys);
    }
    keys.add(key);
  };
  const addAncestors = (ids: ReadonlySet<string>) => {
    for (const id of ids) {
      const node = data.nodes.find((candidate) => candidate.id === id);
      if (node !== undefined) add(levelRowId(node.level), `n${id}`);
    }
  };
  const primaryKey = selectionKey(selection);
  addAncestors(chainIds(selection, data.nodes, data.units));
  if (selection.kind === "unit") {
    // The block and its bar are one thing in two rows: whichever the cursor is in, the other is linked.
    const drawn = data.units.some((unit) => unit.context_total !== null && selectionKey({ kind: "unit", i0: unit.i0, i1: unit.i1, unitKind: unit.kind }) === primaryKey);
    if (drawn) add(row === INPUT_ROW ? UNITS_ROW : INPUT_ROW, primaryKey);
  }
  if (row === null) return { primary: null, linked };
  const own = linked.get(row);
  own?.delete(primaryKey);
  if (own?.size === 0) linked.delete(row);
  return { primary: { row, key: primaryKey }, linked };
}

/** The viewport that shows [startMs, endMs] when `view` does not: the same width, centred on it, inside the base extent; `view` itself when it already shows some of it. */
export function revealView(axis: AxisMap, view: Viewport, base: Viewport, startMs: number, endMs: number): Viewport {
  const u0 = axis.toU(startMs);
  const u1 = axis.toU(endMs);
  const shown = axis.viewU(view);
  if (u1 >= shown.from && u0 <= shown.to) return view;
  const width = shown.to - shown.from;
  const baseU = axis.viewU(base);
  const from = Math.min(Math.max((u0 + u1) / 2 - width / 2, baseU.from), baseU.to - width);
  return axis.viewFromU({ from, to: from + width });
}
