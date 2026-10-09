// Pure model of the run-timeline page: where a span sits on the window's axis,
// which raw-message parts belong to a unit, and how the arrow keys move over the rows.
// No React, no I/O.

import type {
  RunTimelineMessagePart,
  RunTimelineNode,
  RunTimelineRequest,
  RunTimelineUnit,
} from "@/lib/contracts/types";
import { categoryColor } from "@/lib/context-colors";

export interface TimelineWindow {
  from: string;
  to: string;
}

export type Selection =
  | { kind: "node"; id: string }
  | { kind: "unit"; i0: number; i1: number; unitKind: RunTimelineUnit["kind"] }
  /** An LLM request: its bar, and every block it read for the first time (`added_from`..`added_to`). */
  | { kind: "request"; idx: number };

export function isSelected(selection: Selection | null, candidate: Selection): boolean {
  if (selection?.kind === "node" && candidate.kind === "node") return selection.id === candidate.id;
  if (selection?.kind === "request" && candidate.kind === "request") return selection.idx === candidate.idx;
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
 * for a layer-0 block, the level-1 node covering it and every ancestor above that; for a request, the
 * same for every block it read. The chain stops where a parent is not in `nodes` (outside the loaded window).
 */
export function chainIds(
  selection: Selection | null,
  nodes: readonly RunTimelineNode[],
  units: readonly RunTimelineUnit[],
  requests: readonly RunTimelineRequest[] = [],
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
  } else if (selection?.kind === "request") {
    const request = requests.find((candidate) => candidate.idx === selection.idx);
    if (request !== undefined) starts.push(...requestUnits(request, units).map((unit) => unit.parent));
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

/** A block narrower than this would vanish; it may grow to it only into free space. */
export const MIN_BLOCK_PX = 3;
/** Below this a block has no visible body and is drawn as a thin marker instead. */
export const MARKER_MIN_BODY_PX = 2;
/** Width of a marker's visible line and of its (slightly larger) hit area, in pixels. */
export const MARKER_LINE_PX = 2;
export const MARKER_HIT_PX = 4;
/** Markers that sit within this distance of each other are stacked in separate lanes along the row's top. */
const MARKER_CLUSTER_PX = 3;
export const MARKER_LANES = 5;

export interface RowItem {
  key: string;
  start: string;
  end: string;
}

/** Where one block of a row is drawn, in pixels from the track's left edge. */
export interface RowPlacement {
  key: string;
  left: number;
  width: number;
  /** No room for a body: drawn as a thin line at `left` (hit area `MARKER_HIT_PX` wide), in `lane`. */
  marker: boolean;
  lane: number;
}

/** One block of a row as a span of axis coordinates. */
export interface SpanItem {
  key: string;
  u0: number;
  u1: number;
}

/**
 * Lays out one row of blocks on a track `trackPx` wide over an axis-coordinate view. A block's body
 * never reaches the next block's start: the minimum width only fills the free space before the next
 * block, and a block with no room at all becomes a marker (a thin line in its own lane) rather than
 * covering its neighbour.
 */
export function layoutSpans(
  items: readonly SpanItem[],
  view: Viewport,
  trackPx: number,
  minPx: number = MIN_BLOCK_PX,
): RowPlacement[] {
  const boxes: { key: string; left: number; right: number }[] = [];
  for (const item of items) {
    const box = projectBox(item.u0, item.u1, view);
    if (box === null) continue;
    const left = (box.left / 100) * trackPx;
    boxes.push({ key: item.key, left, right: left + (box.width / 100) * trackPx });
  }
  boxes.sort((a, b) => a.left - b.left || a.right - b.right);
  const placements: RowPlacement[] = [];
  let lastMarker: { left: number; lane: number } | null = null;
  boxes.forEach((box, i) => {
    const natural = box.right - box.left;
    const limit = i + 1 < boxes.length ? boxes[i + 1].left : trackPx;
    const room = Math.max(0, limit - box.left);
    const width = Math.min(natural >= minPx ? natural : Math.max(natural, Math.min(minPx, room)), trackPx - box.left);
    if (width >= MARKER_MIN_BODY_PX) {
      placements.push({ key: box.key, left: box.left, width, marker: false, lane: 0 });
      return;
    }
    const near = lastMarker !== null && box.left - lastMarker.left < MARKER_CLUSTER_PX;
    const lane = near && lastMarker !== null ? (lastMarker.lane + 1) % MARKER_LANES : 0;
    lastMarker = { left: box.left, lane };
    placements.push({ key: box.key, left: box.left, width: 0, marker: true, lane });
  });
  return placements;
}

/** A block drawn narrower than this gets no border or rounding: it is drawn as a fill, one per pixel column. */
export const NARROW_DRAW_PX = 4;

/** `layoutSpans` for blocks given by time, over a time window. */
export function layoutRow(
  items: readonly RowItem[],
  window: TimelineWindow,
  trackPx: number,
  minPx: number = MIN_BLOCK_PX,
): RowPlacement[] {
  return layoutSpans(
    items.map((item) => ({ key: item.key, u0: Date.parse(item.start), u1: Date.parse(item.end) })),
    viewportOf(window),
    trackPx,
    minPx,
  );
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
  requests: readonly RunTimelineRequest[] = [],
): { nodeIds: Set<string>; unitKeys: Set<string> } {
  if (hover === null) return { nodeIds: new Set(), unitKeys: new Set() };
  if (hover.kind === "request") {
    const request = requests.find((candidate) => candidate.idx === hover.idx);
    const covered = request === undefined ? [] : requestUnits(request, units);
    return { nodeIds: chainIds(hover, nodes, units, requests), unitKeys: new Set(covered.map(unitKey)) };
  }
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
 * The message index the context breakdown follows: a selected block's own request (the first at or
 * after it), a selected node's first; with nothing selected, the last request sent inside the viewport
 * (else the last one before it, else the first). Null when the agent made no request.
 */
export function contextPoint(
  selection: Selection | null,
  nodes: readonly RunTimelineNode[],
  requests: readonly RunTimelineRequest[],
  view: Viewport,
): number | null {
  if (selection?.kind === "request") return selection.idx;
  if (selection?.kind === "unit") return selection.i0;
  if (selection?.kind === "node") return nodes.find((node) => node.id === selection.id)?.span_start ?? null;
  const sent = requests.map((request) => ({ request, at: Date.parse(request.ts) }));
  const inside = sent.filter(({ at }) => at >= view.from && at <= view.to);
  const before = sent.filter(({ at }) => at < view.from);
  const pick = inside.at(-1) ?? before.at(-1) ?? sent.at(0);
  return pick?.request.idx ?? null;
}

/** The largest newly added context among the requests: what the added-context row scales to. */
export function maxAdded(requests: readonly RunTimelineRequest[]): number {
  return requests.reduce((top, request) => Math.max(top, request.added_tokens), 0);
}

/** The largest input size among the requests: what the context-size row scales to. */
export function maxInput(requests: readonly RunTimelineRequest[]): number {
  return requests.reduce((top, request) => Math.max(top, request.input_tokens), 0);
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

/** A request bar is never thinner than this, and keeps this gap to each side of the blocks it spans, in pixels. */
export const BAR_MIN_PX = 2;
export const BAR_GAP_PX = 1;

/** Whether a request first read the block: the block starts inside its added message range. */
export function requestCovers(request: Pick<RunTimelineRequest, "added_from" | "added_to">, unit: Pick<RunTimelineUnit, "i0">): boolean {
  return request.added_from <= unit.i0 && unit.i0 < request.added_to;
}

const byFirstMessage = new WeakMap<readonly RunTimelineUnit[], RunTimelineUnit[]>();

/** The blocks a request read for the first time, in message order (found by binary search over the blocks sorted once per data). */
export function requestUnits(request: Pick<RunTimelineRequest, "added_from" | "added_to">, units: readonly RunTimelineUnit[]): RunTimelineUnit[] {
  let sorted = byFirstMessage.get(units);
  if (sorted === undefined) {
    sorted = [...units].sort((a, b) => a.i0 - b.i0 || a.i1 - b.i1);
    byFirstMessage.set(units, sorted);
  }
  let lo = 0;
  let hi = sorted.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sorted[mid].i0 < request.added_from) lo = mid + 1;
    else hi = mid;
  }
  const out: RunTimelineUnit[] = [];
  for (let i = lo; i < sorted.length && sorted[i].i0 < request.added_to; i += 1) out.push(sorted[i]);
  return out;
}

/** The request that first read a block, if it is among `requests`. */
export function requestReading(unit: Pick<RunTimelineUnit, "i0">, requests: readonly RunTimelineRequest[]): RunTimelineRequest | undefined {
  return requests.find((request) => requestCovers(request, unit));
}

/**
 * Where a request's bar sits in axis coordinates: from the start of the first block it read to the end of
 * the last, on either axis. A request with no block of its own in the data sits at its send time.
 */
export function requestSpan(
  request: RunTimelineRequest,
  units: readonly RunTimelineUnit[],
  axis: Pick<AxisMap, "toU" | "unitSpan">,
  covered: readonly RunTimelineUnit[] = requestUnits(request, units),
): AxisSpan {
  if (covered.length === 0) {
    const u = axis.toU(Date.parse(request.ts));
    return { u0: u, u1: u };
  }
  let u0 = Infinity;
  let u1 = -Infinity;
  for (const unit of covered) {
    const span = axis.unitSpan(unit);
    u0 = Math.min(u0, span.u0);
    u1 = Math.max(u1, span.u1);
  }
  return { u0, u1 };
}

/** A bar's left edge and width in pixels on a track `trackPx` wide showing `viewU`: the span less the gap each side, at least `BAR_MIN_PX`. */
export function barBox(span: AxisSpan, viewU: { from: number; to: number }, trackPx: number): { left: number; width: number } {
  const scale = trackPx / (viewU.to - viewU.from);
  const left = (span.u0 - viewU.from) * scale;
  const right = (span.u1 - viewU.from) * scale;
  return { left: left + BAR_GAP_PX, width: Math.max(right - left - 2 * BAR_GAP_PX, BAR_MIN_PX) };
}

const REQUEST_UNIT_ORDER: readonly RunTimelineUnit["kind"][] = ["thinking", "text", "call"];

/**
 * The block of the AIMessage that made a request (its thinking, else its text, else its call block):
 * what the details pane shows while the request is selected. Null when that message has no block.
 */
export function requestSelection(request: Pick<RunTimelineRequest, "idx">, units: readonly RunTimelineUnit[]): Selection | null {
  const own = units.filter((unit) => unit.i0 === request.idx);
  const pick =
    REQUEST_UNIT_ORDER.map((kind) => own.find((unit) => unit.kind === kind)).find((unit) => unit !== undefined) ??
    units.find((unit) => unit.i0 <= request.idx && request.idx <= unit.i1);
  return pick === undefined ? null : { kind: "unit", i0: pick.i0, i1: pick.i1, unitKind: pick.kind };
}

/** Whether a request's bar is lit: selected (itself, or a block it read) or hovered (the same). */
export function requestLit(
  request: Pick<RunTimelineRequest, "idx" | "added_from" | "added_to">,
  selection: Selection | null,
  hover: Hover | null,
): { selected: boolean; hovered: boolean } {
  const lit = (target: Selection | null) =>
    (target?.kind === "request" && target.idx === request.idx) ||
    (target?.kind === "unit" && requestCovers(request, { i0: target.i0 }));
  return { selected: lit(selection), hovered: lit(hover) };
}
