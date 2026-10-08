// Pure model of the run-timeline page: where a span sits on the window's axis,
// which raw-message parts belong to a unit, and how the arrow keys move over the rows.
// No React, no I/O.

import type {
  RunTimelineMessagePart,
  RunTimelineNode,
  RunTimelineRequest,
  RunTimelineUnit,
  RunTimelineUsage,
} from "@/lib/contracts/types";
import { categoryColor } from "@/lib/context-colors";
import { formatTokensCompact } from "@/lib/format/format-number";

export interface TimelineWindow {
  from: string;
  to: string;
}

/** One step of the zoom path: the window a session zoom narrowed to. */
export interface Crumb extends TimelineWindow {
  label: string;
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

/**
 * The stretches of a level's row that are not yet summarized: the nodes one level down that have no
 * parent (the tree is built bottom-up, so the tail of each level waits for its group to close).
 * Contiguous ones are merged into one stretch.
 */
export function pendingSpans(
  nodes: readonly RunTimelineNode[],
  level: number,
): TimelineWindow[] {
  const open = nodes
    .filter((node) => node.level === level - 1 && node.parent === null)
    .sort((a, b) => a.span_start - b.span_start);
  const spans: (TimelineWindow & { last: number })[] = [];
  for (const node of open) {
    const tail = spans.at(-1);
    if (tail !== undefined && node.span_start === tail.last + 1) {
      tail.to = node.end;
      tail.last = node.span_end;
    } else {
      spans.push({ from: node.start, to: node.end, last: node.span_end });
    }
  }
  return spans.map(({ from, to }) => ({ from, to }));
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

/** A block's token count as drawn on it: `~` marks an estimate. Null while no request has read it. */
export function tokenLabel(tokens: number | null, estimated: boolean | null): string | null {
  return tokens === null ? null : `${estimated === true ? "~" : ""}${formatTokensCompact(tokens)}`;
}

/** Pixels one character of a block's token label takes, and the padding around it. */
const TOKEN_CHAR_PX = 6;
const TOKEN_PAD_PX = 6;

/** Whether the token label fits a block `widthPx` wide while leaving `roomPx` for what else it shows. */
export function tokenFits(label: string, widthPx: number, roomPx = 0): boolean {
  return widthPx >= label.length * TOKEN_CHAR_PX + TOKEN_PAD_PX + roomPx;
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

/** The share of input tokens served from cache; null when there was no input. */
export function cacheHitRate(usage: Pick<RunTimelineUsage, "input" | "cache_read">): number | null {
  return usage.input > 0 ? usage.cache_read / usage.input : null;
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

export type AxisMode = "hybrid" | "time";

/** Where one block sits in axis coordinates. */
export interface AxisSpan {
  u0: number;
  u1: number;
}

/**
 * The map from time to the x coordinate every row shares. Axis coordinates `u` are an arbitrary
 * monotone unit (milliseconds since the base start on the time axis, token-like weight on the hybrid
 * axis); rows only ever compare them against `viewU` of the current viewport.
 */
export interface AxisMap {
  mode: AxisMode;
  /** Length of the whole base extent in axis coordinates. */
  total: number;
  /** The narrowest viewport a zoom may reach, in axis coordinates. */
  minSpan: number;
  /** Time to axis coordinate; at an instant that a block of no duration stretches over, `lo` is its left edge and `hi` its right. */
  toU: (ms: number, side?: "lo" | "hi") => number;
  fromU: (u: number) => number;
  /** The viewport in axis coordinates. */
  viewU: (view: Viewport) => Viewport;
  /** A viewport in axis coordinates back to time (at least 1 ms wide). */
  viewFromU: (view: Viewport) => Viewport;
  unitSpan: (unit: Pick<RunTimelineUnit, "kind" | "i0" | "i1" | "start" | "end">) => AxisSpan;
  /** A node follows the blocks its message span covers; with none loaded, its own times. */
  nodeSpan: (node: Pick<RunTimelineNode, "start" | "end" | "span_start" | "span_end">) => AxisSpan;
  /** Hybrid only: the left edge of every block with the time it starts at, for tick labels. */
  boundaries: readonly { ms: number; u: number }[];
}

/** Width of a block with no token count (not yet in any request) or a tiny one, as a share of all block weight. */
export const MIN_BLOCK_SHARE = 0.003;
/** The idle gaps together take this share of the weight of all blocks; each gap is proportional to log(1 + idle seconds). */
export const GAP_SHARE = 0.25;
/** The narrowest hybrid viewport, as a share of the whole axis. */
const MIN_VIEW_SHARE = 1 / 2000;

interface Knot {
  ms: number;
  u: number;
}

function knotToU(knots: readonly Knot[], ms: number, side: "lo" | "hi"): number {
  const first = knots[0];
  const last = knots[knots.length - 1];
  if (ms <= first.ms) return first.u;
  if (ms >= last.ms) return last.u;
  // lo: the first knot at or after ms; hi: the last knot at or before ms.
  let l = 0;
  let r = knots.length - 1;
  while (l < r) {
    const mid = (l + r) >> 1;
    if (side === "lo" ? knots[mid].ms >= ms : knots[mid].ms > ms) r = mid;
    else l = mid + 1;
  }
  const hit = side === "lo" ? l : l - 1;
  if (knots[hit].ms === ms) return knots[hit].u;
  const a = side === "lo" ? knots[hit - 1] : knots[hit];
  const b = side === "lo" ? knots[hit] : knots[hit + 1];
  return a.u + ((ms - a.ms) / (b.ms - a.ms)) * (b.u - a.u);
}

function knotFromU(knots: readonly Knot[], u: number): number {
  const first = knots[0];
  const last = knots[knots.length - 1];
  if (u <= first.u) return first.ms;
  if (u >= last.u) return last.ms;
  let l = 0;
  let r = knots.length - 1;
  while (l < r) {
    const mid = (l + r) >> 1;
    if (knots[mid].u >= u) r = mid;
    else l = mid + 1;
  }
  const a = knots[l - 1];
  const b = knots[l];
  return b.u === a.u ? b.ms : a.ms + ((u - a.u) / (b.u - a.u)) * (b.ms - a.ms);
}

function finishAxis(
  mode: AxisMode,
  knots: Knot[],
  minSpan: number,
  spans: Map<string, AxisSpan>,
  units: readonly RunTimelineUnit[],
  boundaries: { ms: number; u: number }[],
): AxisMap {
  const toU = (ms: number, side: "lo" | "hi" = "lo") => knotToU(knots, ms, side);
  const fromU = (u: number) => knotFromU(knots, u);
  const timeSpan = (start: string, end: string): AxisSpan => ({
    u0: toU(Date.parse(start), "lo"),
    u1: toU(Date.parse(end), "hi"),
  });
  const unitSpan: AxisMap["unitSpan"] = (unit) => spans.get(unitKey(unit)) ?? timeSpan(unit.start, unit.end);
  return {
    mode,
    total: knots[knots.length - 1].u,
    minSpan,
    toU,
    fromU,
    viewU: (view) => ({ from: toU(view.from, "lo"), to: toU(view.to, "hi") }),
    viewFromU: (view) => {
      const from = fromU(view.from);
      return { from, to: Math.max(fromU(view.to), from + 1) };
    },
    unitSpan,
    nodeSpan: (node) => {
      if (mode === "time") return timeSpan(node.start, node.end);
      let u0 = Infinity;
      let u1 = -Infinity;
      for (const unit of units) {
        if (unit.i0 < node.span_start || unit.i0 > node.span_end) continue;
        const span = spans.get(unitKey(unit));
        if (span === undefined) continue;
        u0 = Math.min(u0, span.u0);
        u1 = Math.max(u1, span.u1);
      }
      return u0 <= u1 ? { u0, u1 } : timeSpan(node.start, node.end);
    },
    boundaries,
  };
}

/**
 * Builds the x map of the timeline over the loaded extent `base`.
 *
 * `time`: linear in time.
 * `hybrid`: the blocks are laid end to end in time order. A block's width is its `context_tokens`
 * (at least `MIN_BLOCK_SHARE` of the total block weight, which is also what a block with no count
 * gets); the space between two blocks (and before the first / after the last, up to the extent's
 * ends) is `k * ln(1 + idle seconds)`, with `k` set so that all gaps together take `GAP_SHARE` of
 * the block weight. Inside a block, and inside a gap, time is linear.
 */
export function buildAxisMap(units: readonly RunTimelineUnit[], base: Viewport, mode: AxisMode): AxisMap {
  const extent = Math.max(base.to - base.from, 1);
  if (mode === "time") {
    const spans = new Map<string, AxisSpan>();
    return finishAxis("time", [{ ms: base.from, u: 0 }, { ms: base.from + extent, u: extent }], MIN_VIEW_MS, spans, units, []);
  }
  const sorted = units
    .map((unit) => ({ unit, start: Date.parse(unit.start), end: Date.parse(unit.end) }))
    .filter(({ start, end }) => !Number.isNaN(start) && !Number.isNaN(end))
    .sort((a, b) => a.start - b.start || a.end - b.end || a.unit.i0 - b.unit.i0);
  const tokens = sorted.map(({ unit }) => Math.max(unit.context_tokens ?? 0, 0));
  const tokenTotal = tokens.reduce((sum, value) => sum + value, 0);
  const floor = MIN_BLOCK_SHARE * (tokenTotal > 0 ? tokenTotal : sorted.length);
  const weights = tokens.map((value) => Math.max(value, floor));
  const blockTotal = weights.reduce((sum, value) => sum + value, 0);

  // Idle before, between and after the blocks, in time order; blocks may overlap, so idle is measured from the furthest end so far.
  const idles: number[] = [];
  const starts: number[] = [];
  const ends: number[] = [];
  let cursor = base.from;
  for (const { start, end } of sorted) {
    const t0 = Math.max(start, cursor);
    idles.push(Math.log1p((t0 - cursor) / 1000));
    starts.push(t0);
    ends.push(Math.max(end, t0));
    cursor = ends[ends.length - 1];
  }
  const trailing = Math.log1p(Math.max(base.to - cursor, 0) / 1000);
  const logTotal = idles.reduce((sum, value) => sum + value, 0) + trailing;
  const k = logTotal > 0 ? (GAP_SHARE * blockTotal) / logTotal : 0;

  const knots: Knot[] = [{ ms: base.from, u: 0 }];
  const spans = new Map<string, AxisSpan>();
  const boundaries: { ms: number; u: number }[] = [];
  let u = 0;
  sorted.forEach(({ unit, start }, i) => {
    u += k * idles[i];
    knots.push({ ms: starts[i], u });
    const u0 = u;
    u += weights[i];
    knots.push({ ms: ends[i], u });
    spans.set(unitKey(unit), { u0, u1: u });
    boundaries.push({ ms: start, u: u0 });
  });
  u += k * trailing;
  knots.push({ ms: Math.max(base.to, cursor), u });
  if (!(u > 0)) return buildAxisMap([], base, "time");
  return finishAxis("hybrid", knots, u * MIN_VIEW_SHARE, spans, units, boundaries);
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
  return projectBox(axis.toU(Date.parse(start), "lo"), axis.toU(Date.parse(end), "hi"), viewU);
}

/** Pixels reserved for one tick label on the hybrid axis. */
const TICK_LABEL_PX = 76;

function boundaryLabel(ms: number, spanMs: number): string {
  const d = new Date(ms);
  const clock = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  if (spanMs >= 6 * HOUR) return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return spanMs < 5 * SECOND ? `${clock}.${pad(d.getMilliseconds(), 3)}` : clock;
}

/**
 * Tick labels of the hybrid axis: the times at which blocks start, left to right, each at its block's
 * edge, keeping only those that leave room for the label before them. A view inside one block (no
 * edge in sight) falls back to round times placed through the map.
 */
export function axisMapTicks(axis: AxisMap, view: Viewport, trackPx: number): AxisTick[] {
  if (axis.mode === "time") return axisTicks(view);
  const viewU = axis.viewU(view);
  const span = viewU.to - viewU.from;
  if (!(span > 0) || !(trackPx > 0)) return [];
  const timeSpan = view.to - view.from;
  const ticks: AxisTick[] = [];
  let lastPx = -Infinity;
  for (const { ms, u } of axis.boundaries) {
    const px = ((u - viewU.from) / span) * trackPx;
    if (px < TICK_LABEL_PX / 2 || px > trackPx - TICK_LABEL_PX / 2 || px - lastPx < TICK_LABEL_PX) continue;
    ticks.push({ left: (px / trackPx) * 100, label: boundaryLabel(ms, timeSpan) });
    lastPx = px;
  }
  if (ticks.length > 0) return ticks;
  return axisTicks(view, Math.max(1, Math.floor(trackPx / TICK_LABEL_PX / 2))).map((tick) => {
    const u = axis.toU(view.from + (tick.left / 100) * timeSpan, "lo");
    return { ...tick, left: ((u - viewU.from) / span) * 100 };
  });
}

/** A request bar is never thinner than this, and keeps this gap to each side of the blocks it spans, in pixels. */
export const BAR_MIN_PX = 2;
export const BAR_GAP_PX = 1;

/** Whether a request first read the block: the block starts inside its added message range. */
export function requestCovers(request: Pick<RunTimelineRequest, "added_from" | "added_to">, unit: Pick<RunTimelineUnit, "i0">): boolean {
  return request.added_from <= unit.i0 && unit.i0 < request.added_to;
}

/** The blocks a request read for the first time, in message order. */
export function requestUnits(request: Pick<RunTimelineRequest, "added_from" | "added_to">, units: readonly RunTimelineUnit[]): RunTimelineUnit[] {
  return units.filter((unit) => requestCovers(request, unit)).sort((a, b) => a.i0 - b.i0 || a.i1 - b.i1);
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
): AxisSpan {
  const covered = requestUnits(request, units);
  if (covered.length === 0) {
    const u = axis.toU(Date.parse(request.ts), "lo");
    return { u0: u, u1: u };
  }
  const spans = covered.map((unit) => axis.unitSpan(unit));
  return { u0: Math.min(...spans.map((span) => span.u0)), u1: Math.max(...spans.map((span) => span.u1)) };
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
