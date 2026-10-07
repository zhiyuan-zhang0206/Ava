// Pure model of the run-timeline page: where a span sits on the window's axis,
// what a drill into a node or unit asks for, and which raw-message parts belong
// to a unit. No React, no I/O.

import type {
  RunTimelineMessagePart,
  RunTimelineNode,
  RunTimelineRequest,
  RunTimelineUnit,
  RunTimelineUsage,
} from "@/lib/contracts/types";
import { categoryColor } from "@/lib/context-colors";

export interface TimelineWindow {
  from: string;
  to: string;
}

/** One step of the drill path: the window it narrowed to. */
export interface Crumb extends TimelineWindow {
  label: string;
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

export type Hover = Selection | { kind: "request"; idx: number };

export function unitKey(unit: Pick<RunTimelineUnit, "kind" | "i0" | "i1">): string {
  return `${unit.kind}-${unit.i0}-${unit.i1}`;
}

/**
 * The ids of the nodes a selection lights up: the selected node and every ancestor above it, or,
 * for a layer-0 block, the level-1 node covering it and every ancestor above that. The chain stops
 * where a parent is not in `nodes` (outside the loaded window).
 */
export function chainIds(
  selection: Selection | null,
  nodes: readonly RunTimelineNode[],
  units: readonly RunTimelineUnit[],
): Set<string> {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  let next: string | null = null;
  if (selection?.kind === "node") next = selection.id;
  else if (selection?.kind === "unit") {
    next =
      units.find(
        (unit) =>
          unit.i0 === selection.i0 && unit.i1 === selection.i1 && unit.kind === selection.unitKind,
      )?.parent ?? null;
  }
  const chain = new Set<string>();
  while (next !== null && !chain.has(next)) {
    const node = byId.get(next);
    if (node === undefined) break;
    chain.add(next);
    next = node.parent;
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

/** A span's box on the window axis as percentages, clamped to the window; null when it lies outside. */
export function spanBox(
  start: string,
  end: string,
  window: TimelineWindow,
): { left: number; width: number } | null {
  const from = Date.parse(window.from);
  const to = Date.parse(window.to);
  const s = Date.parse(start);
  const e = Date.parse(end);
  if (!(to > from) || Number.isNaN(s) || Number.isNaN(e) || e < from || s > to) return null;
  const left = (Math.max(s, from) - from) / (to - from);
  const right = (Math.min(e, to) - from) / (to - from);
  return { left: left * 100, width: Math.max(0, right - left) * 100 };
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

/**
 * Lays out one row of blocks on a track `trackPx` wide. A block's body never reaches the next
 * block's start: the minimum width only fills the free space before the next block, and a block with
 * no room at all becomes a marker (a thin line in its own lane) rather than covering its neighbour.
 */
export function layoutRow(
  items: readonly RowItem[],
  window: TimelineWindow,
  trackPx: number,
  minPx: number = MIN_BLOCK_PX,
): RowPlacement[] {
  const boxes: { key: string; left: number; right: number }[] = [];
  for (const item of items) {
    const box = spanBox(item.start, item.end, window);
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

/** The window of a drill into a node: exactly its time span. */
export function nodeWindow(node: RunTimelineNode): TimelineWindow {
  return { from: node.start, to: node.end };
}

const MIN_DRILL_MS = 1000;

/** The window of a drill into a unit; a unit with no extent (one instant) gets one second so the window is valid. */
export function unitWindow(unit: RunTimelineUnit): TimelineWindow {
  const start = Date.parse(unit.start);
  const end = Date.parse(unit.end);
  if (end - start >= MIN_DRILL_MS) return { from: unit.start, to: unit.end };
  return { from: unit.start, to: new Date(start + MIN_DRILL_MS).toISOString() };
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
export function zoomViewport(view: Viewport, base: Viewport, frac: number, factor: number): Viewport {
  const span = view.to - view.from;
  const floor = Math.min(MIN_VIEW_MS, base.to - base.from);
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
  if (hover === null || hover.kind === "request") return { nodeIds: new Set(), unitKeys: new Set() };
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
  if (selection?.kind === "unit") return selection.i0;
  if (selection?.kind === "node") return nodes.find((node) => node.id === selection.id)?.span_start ?? null;
  const sent = requests.map((request) => ({ request, at: Date.parse(request.ts) }));
  const inside = sent.filter(({ at }) => at >= view.from && at <= view.to);
  const before = sent.filter(({ at }) => at < view.from);
  const pick = inside.at(-1) ?? before.at(-1) ?? sent.at(0);
  return pick?.request.idx ?? null;
}

/** The largest input size among the requests: what the context-size row scales to. */
export function maxInput(requests: readonly RunTimelineRequest[]): number {
  return requests.reduce((top, request) => Math.max(top, request.input_tokens), 0);
}
