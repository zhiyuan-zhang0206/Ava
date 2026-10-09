// Pure model of the run-timeline page: where a span sits on the window's axis,
// which raw-message parts belong to a unit, and how the arrow keys move over the rows.
// No React, no I/O.

import type {
  RunTimelineMessagePart,
  RunTimelineNode,
  RunTimelineUnit,
} from "@/lib/contracts/types";
import { categoryColor } from "@/lib/context-colors";

export interface TimelineWindow {
  from: string;
  to: string;
}

export type Selection =
  | { kind: "node"; id: string }
  | { kind: "unit"; i0: number; i1: number; unitKind: RunTimelineUnit["kind"] };

export function isSelected(selection: Selection | null, candidate: Selection): boolean {
  if (selection?.kind === "node" && candidate.kind === "node") return selection.id === candidate.id;
  if (selection?.kind === "unit" && candidate.kind === "unit") {
    return (
      selection.i0 === candidate.i0 &&
      selection.i1 === candidate.i1 &&
      selection.unitKind === candidate.unitKind
    );
  }
  return false;
}

export type Hover = Selection;

export function unitKey(unit: Pick<RunTimelineUnit, "kind" | "i0" | "i1">): string {
  return `${unit.kind}-${unit.i0}-${unit.i1}`;
}

/**
 * The ids of the nodes a selection lights up: the selected node and every ancestor above it, or,
 * for a layer-0 block, the level-1 node covering it and every ancestor above that. The chain stops where a parent is not in `nodes` (outside the loaded window).
 */
export function chainIds(
  selection: Selection | null,
  nodes: readonly RunTimelineNode[],
  units: readonly RunTimelineUnit[],
): Set<string> {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const starts: (string | null)[] = [];
  if (selection?.kind === "node") starts.push(selection.id);
  else if (selection?.kind === "unit") {
    starts.push(
      units.find(
        (unit) =>
          unit.i0 === selection.i0 && unit.i1 === selection.i1 && unit.kind === selection.unitKind,
      )?.parent ?? null,
    );
  }
  const chain = new Set<string>();
  for (let next = starts.shift(); next !== undefined; next = starts.shift()) {
    while (next !== null && !chain.has(next)) {
      const node = byId.get(next);
      if (node === undefined) break;
      chain.add(next);
      next = node.parent;
    }
  }
  return chain;
}

/** The distinct node levels, topmost first (level 1 = the leaves, drawn last). */
export function levelsTopFirst(nodes: readonly RunTimelineNode[]): number[] {
  return [...new Set(nodes.map((node) => node.level))].sort((a, b) => b - a);
}

/** A span's box on an axis-coordinate view as percentages, clamped to the view; null when it lies outside. */
export function projectBox(
  u0: number,
  u1: number,
  view: Viewport,
): { left: number; width: number } | null {
  if (!(view.to > view.from) || Number.isNaN(u0) || Number.isNaN(u1) || u1 < view.from || u0 > view.to) return null;
  const span = view.to - view.from;
  const left = (Math.max(u0, view.from) - view.from) / span;
  const right = (Math.min(u1, view.to) - view.from) / span;
  return { left: left * 100, width: Math.max(0, right - left) * 100 };
}

/** A span's box on the window axis as percentages, clamped to the window; null when it lies outside. */
export function spanBox(
  start: string,
  end: string,
  window: TimelineWindow,
): { left: number; width: number } | null {
  return projectBox(Date.parse(start), Date.parse(end), viewportOf(window));
}

/** No item is drawn narrower than this, in pixels: an instant (a call point, an inbound message) is as wide as this. */
export const MIN_ITEM_PX = 3;

/** An item drawn narrower than this gets no border or rounding: it is a fill, one per pixel column. */
export const NARROW_DRAW_PX = 4;

/** Where one item of a row is drawn, in pixels from the track's left edge. */
export interface RowPlacement {
  key: string;
  left: number;
  width: number;
}

/** One item of a row as a span of axis coordinates. */
export interface SpanItem {
  key: string;
  u0: number;
  u1: number;
}

/**
 * Lays out one row on a track `trackPx` wide over an axis-coordinate view. An item starts exactly where
 * its time is; only its drawn width is at least `minPx` (the axis itself stays linear), so items closer
 * than that overlap, which the row's column merge (`aggregateColumns`) resolves. An item running past an
 * edge is cut there.
 */
export function layoutSpans(
  items: readonly SpanItem[],
  view: Viewport,
  trackPx: number,
  minPx: number = MIN_ITEM_PX,
): RowPlacement[] {
  const placements: RowPlacement[] = [];
  for (const item of items) {
    const box = projectBox(item.u0, item.u1, view);
    if (box === null) continue;
    const left = (box.left / 100) * trackPx;
    const width = Math.min(Math.max((box.width / 100) * trackPx, minPx), trackPx - left);
    placements.push({ key: item.key, left, width });
  }
  return placements.sort((a, b) => a.left - b.left || a.width - b.width);
}

export function firstLine(text: string, max: number): string {
  const line = text.split("\n").find((candidate) => candidate.trim() !== "") ?? "";
  const trimmed = line.trim();
  return trimmed.length > max ? `${trimmed.slice(0, max - 1)}…` : trimmed;
}

/** The raw-message part kind a block shows; inbound and note blocks show their messages whole. */
const PART_OF_KIND: Partial<Record<RunTimelineUnit["kind"], RunTimelineMessagePart["kind"]>> = {
  thinking: "think",
  text: "text",
  call: "call",
  output: "out",
};

/** A block's own parts of the raw messages it spans: thinking, text, tool call and tool output
 *  are one kind of part each; inbound and note blocks are whole. */
export function partsForUnit(
  kind: RunTimelineUnit["kind"],
  parts: readonly RunTimelineMessagePart[],
): RunTimelineMessagePart[] {
  const wanted = PART_OF_KIND[kind];
  return wanted === undefined ? [...parts] : parts.filter((part) => part.kind === wanted);
}

/** The legend kinds, in drawing order: a block's kind, inbound split by sender. */
export type BlockClass =
  | "human"
  | "agent"
  | "text"
  | "thinking"
  | "call"
  | "output"
  | "note";
export const BLOCK_CLASSES: readonly BlockClass[] = [
  "human",
  "agent",
  "text",
  "thinking",
  "call",
  "output",
  "note",
];

export function blockClass(unit: Pick<RunTimelineUnit, "kind" | "source">): BlockClass {
  if (unit.kind === "inbound") return unit.source?.startsWith("agent:") ? "agent" : "human";
  return unit.kind;
}

const CLASS_CATEGORY: Record<BlockClass, string> = {
  human: "user_input",
  agent: "agent_messages",
  text: "output",
  thinking: "reasoning",
  call: "tool_call",
  output: "tool_response",
  note: "system_notes",
};

/** The color of a kind of block: the context breakdown's color of the same kind of content. */
export function classColor(kind: BlockClass): string {
  return categoryColor(CLASS_CATEGORY[kind]);
}

export function unitColor(unit: Pick<RunTimelineUnit, "kind" | "source">): string {
  return classColor(blockClass(unit));
}

/** The visible part of the loaded data on the time axis, in epoch milliseconds. */
export interface Viewport {
  from: number;
  to: number;
}

/** The narrowest viewport a zoom may reach: enough to separate neighbouring message units. */
export const MIN_VIEW_MS = 100;

export function viewportOf(window: TimelineWindow): Viewport {
  return { from: Date.parse(window.from), to: Date.parse(window.to) };
}

export function viewportWindow(view: Viewport): TimelineWindow {
  return { from: new Date(view.from).toISOString(), to: new Date(view.to).toISOString() };
}

/** Keeps a viewport of unchanged span inside the base extent; one wider than the base becomes the base. */
export function clampViewport(view: Viewport, base: Viewport): Viewport {
  const span = view.to - view.from;
  if (span >= base.to - base.from) return base;
  const from = Math.min(Math.max(view.from, base.from), base.to - span);
  return { from, to: from + span };
}

/** Scales the viewport span by `factor` (below 1 zooms in) keeping the instant at `frac` (0..1 across the view) fixed. */
export function zoomViewport(
  view: Viewport,
  base: Viewport,
  frac: number,
  factor: number,
  minSpan: number = MIN_VIEW_MS,
): Viewport {
  const span = view.to - view.from;
  const floor = Math.min(minSpan, base.to - base.from);
  const next = Math.min(Math.max(span * factor, floor), base.to - base.from);
  const anchor = view.from + Math.min(Math.max(frac, 0), 1) * span;
  const from = anchor - Math.min(Math.max(frac, 0), 1) * next;
  return clampViewport({ from, to: from + next }, base);
}

/** Shifts the viewport by `fraction` of its own span (positive = later), staying inside the base extent. */
export function panViewport(view: Viewport, base: Viewport, fraction: number): Viewport {
  const delta = (view.to - view.from) * fraction;
  return clampViewport({ from: view.from + delta, to: view.to + delta }, base);
}

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const TICK_STEPS = [
  1, 2, 5, 10, 20, 50, 100, 200, 500, SECOND, 2 * SECOND, 5 * SECOND, 10 * SECOND, 15 * SECOND,
  30 * SECOND, MINUTE, 2 * MINUTE, 5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE, HOUR,
  2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY,
];

export interface AxisTick {
  /** Position across the viewport, 0..100. */
  left: number;
  label: string;
}

function pad(value: number, width = 2): string {
  return String(value).padStart(width, "0");
}

function tickLabel(ms: number, step: number): string {
  const d = new Date(ms);
  const clock = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (step >= DAY) return `${d.getMonth() + 1}/${d.getDate()}`;
  if (step >= MINUTE) return step >= HOUR * 6 ? `${d.getMonth() + 1}/${d.getDate()} ${clock}` : clock;
  const seconds = `${clock}:${pad(d.getSeconds())}`;
  return step >= SECOND ? seconds : `${seconds}.${pad(d.getMilliseconds(), 3)}`;
}

/** Round-number ticks (local time) across the viewport, at most about `target` of them. */
export function axisTicks(view: Viewport, target = 6): AxisTick[] {
  const span = view.to - view.from;
  if (!(span > 0)) return [];
  const step = TICK_STEPS.find((candidate) => span / candidate <= target) ?? DAY;
  // Day-and-longer steps align to local midnight, shorter ones to the local clock.
  const offset = step >= HOUR ? new Date(view.from).getTimezoneOffset() * MINUTE : 0;
  const ticks: AxisTick[] = [];
  for (let at = Math.ceil((view.from - offset) / step) * step + offset; at <= view.to; at += step) {
    ticks.push({ left: ((at - view.from) / span) * 100, label: tickLabel(at, step) });
  }
  return ticks;
}

/** What the legend (or a context breakdown row) highlights: every block of one class, optionally of one source only. */
export interface Highlight {
  cls: BlockClass;
  source: string | null;
}

export function matchesHighlight(unit: Pick<RunTimelineUnit, "kind" | "source">, highlight: Highlight): boolean {
  return blockClass(unit) === highlight.cls && (highlight.source === null || unit.source === highlight.source);
}

/** The distinct sources of the inbound blocks of one class, in order of first appearance. */
export function inboundSources(units: readonly RunTimelineUnit[], cls: "human" | "agent"): string[] {
  const sources: string[] = [];
  for (const unit of units) {
    if (unit.source !== null && blockClass(unit) === cls && !sources.includes(unit.source)) sources.push(unit.source);
  }
  return sources;
}

/** The context breakdown category a block class is drawn from, and back (a category with no blocks maps to null). */
export function classCategory(cls: BlockClass): string {
  return CLASS_CATEGORY[cls];
}

export function categoryClass(category: string): BlockClass | null {
  return BLOCK_CLASSES.find((cls) => CLASS_CATEGORY[cls] === category) ?? null;
}

/**
 * What hovering lights up, more softly than a selection: the hovered block's ancestor chain; for a
 * node also the blocks its message span covers.
 */
export function hoverLit(
  hover: Hover | null,
  nodes: readonly RunTimelineNode[],
  units: readonly RunTimelineUnit[],
): { nodeIds: Set<string>; unitKeys: Set<string> } {
  if (hover === null) return { nodeIds: new Set(), unitKeys: new Set() };
  const nodeIds = chainIds(hover, nodes, units);
  const node = hover.kind === "node" ? nodes.find((candidate) => candidate.id === hover.id) : undefined;
  const covered =
    node === undefined ? [] : units.filter((unit) => unit.i0 >= node.span_start && unit.i0 <= node.span_end);
  return { nodeIds, unitKeys: new Set(covered.map(unitKey)) };
}

/** A node's ancestors, nearest first, as far as they are loaded. */
export function nodeAncestors(node: RunTimelineNode, nodes: readonly RunTimelineNode[]): RunTimelineNode[] {
  const byId = new Map(nodes.map((candidate) => [candidate.id, candidate]));
  const out: RunTimelineNode[] = [];
  let next = node.parent === null ? undefined : byId.get(node.parent);
  while (next !== undefined && !out.includes(next)) {
    out.push(next);
    next = next.parent === null ? undefined : byId.get(next.parent);
  }
  return out;
}

export function nodeChildren(node: RunTimelineNode, nodes: readonly RunTimelineNode[]): RunTimelineNode[] {
  return nodes.filter((candidate) => candidate.parent === node.id).sort((a, b) => a.span_start - b.span_start);
}

/**
 * The message index the context breakdown follows: a selected block's own (the card reads the request
 * at or after it), a selected node's first; with nothing selected, the last LLM request (the first
 * block of an AIMessage with its usage) sent inside the viewport, else the last one before it, else
 * the first. Null when the agent made no request.
 */
export function contextPoint(
  selection: Selection | null,
  nodes: readonly RunTimelineNode[],
  units: readonly RunTimelineUnit[],
  view: Viewport,
): number | null {
  if (selection?.kind === "unit") return selection.i0;
  if (selection?.kind === "node") return nodes.find((node) => node.id === selection.id)?.span_start ?? null;
  const requests = new Map<number, RunTimelineUnit>();
  for (const unit of units) {
    const seen = requests.get(unit.i0);
    if (unit.request !== null && (seen === undefined || unit.start < seen.start)) requests.set(unit.i0, unit);
  }
  const sent = [...requests.values()]
    .map((unit) => ({ unit, at: Date.parse(unit.start) }))
    .sort((a, b) => a.unit.i0 - b.unit.i0);
  const inside = sent.filter(({ at }) => at >= view.from && at <= view.to);
  const before = sent.filter(({ at }) => at < view.from);
  const pick = inside.at(-1) ?? before.at(-1) ?? sent.at(0);
  return pick?.unit.i0 ?? null;
}

/** The blocks the Context size row draws: those a request has read. */
export function contextUnits(units: readonly RunTimelineUnit[]): RunTimelineUnit[] {
  return units.filter((unit) => unit.context_total !== null);
}

/** The largest context total among the blocks: what the Context size row scales to. */
export function maxContextTotal(units: readonly RunTimelineUnit[]): number {
  return units.reduce((top, unit) => Math.max(top, unit.context_total ?? 0), 0);
}

/** The largest block by tokens: what a Messages row that draws heights by tokens scales to. */
export function maxBlockTokens(units: readonly RunTimelineUnit[]): number {
  return units.reduce((top, unit) => Math.max(top, unit.context_tokens ?? 0), 0);
}

/** Where one block sits in axis coordinates. */
export interface AxisSpan {
  u0: number;
  u1: number;
}

/**
 * The map from time to the x coordinate every row of every agent shares. Axis coordinates `u` are
 * milliseconds since the base start; rows only ever compare them against `viewU` of the current viewport.
 */
export interface AxisMap {
  /** Length of the whole base extent in axis coordinates. */
  total: number;
  /** The narrowest viewport a zoom may reach, in axis coordinates. */
  minSpan: number;
  /** Time to axis coordinate, clamped to the base extent. */
  toU: (ms: number) => number;
  fromU: (u: number) => number;
  /** The viewport in axis coordinates. */
  viewU: (view: Viewport) => Viewport;
  /** A viewport in axis coordinates back to time (at least 1 ms wide). */
  viewFromU: (view: Viewport) => Viewport;
  unitSpan: (unit: Pick<RunTimelineUnit, "start" | "end">) => AxisSpan;
  nodeSpan: (node: Pick<RunTimelineNode, "start" | "end">) => AxisSpan;
}

/** The x map of the timeline over the loaded extent `base`: linear in time. */
export function timeAxis(base: Viewport): AxisMap {
  const total = Math.max(base.to - base.from, 1);
  const toU = (ms: number) => Math.min(Math.max(ms - base.from, 0), total);
  const fromU = (u: number) => base.from + Math.min(Math.max(u, 0), total);
  const span = (start: string, end: string): AxisSpan => ({ u0: toU(Date.parse(start)), u1: toU(Date.parse(end)) });
  return {
    total,
    minSpan: MIN_VIEW_MS,
    toU,
    fromU,
    viewU: (view) => ({ from: toU(view.from), to: toU(view.to) }),
    viewFromU: (view) => {
      const from = fromU(view.from);
      return { from, to: Math.max(fromU(view.to), from + 1) };
    },
    unitSpan: (unit) => span(unit.start, unit.end),
    nodeSpan: (node) => span(node.start, node.end),
  };
}

/** Zooms a time viewport by `factor` around the point `frac` across the view, in the axis's own coordinates. */
export function zoomView(axis: AxisMap, view: Viewport, base: Viewport, frac: number, factor: number): Viewport {
  return axis.viewFromU(zoomViewport(axis.viewU(view), axis.viewU(base), frac, factor, axis.minSpan));
}

/** Pans a time viewport by `fraction` of its width on the axis (positive = later). */
export function panView(axis: AxisMap, view: Viewport, base: Viewport, fraction: number): Viewport {
  return axis.viewFromU(panViewport(axis.viewU(view), axis.viewU(base), fraction));
}

/** A time span's box on the current view of an axis, as percentages; null when it lies outside. */
export function axisBox(
  axis: AxisMap,
  start: string,
  end: string,
  viewU: Viewport,
): { left: number; width: number } | null {
  return projectBox(axis.toU(Date.parse(start)), axis.toU(Date.parse(end)), viewU);
}
