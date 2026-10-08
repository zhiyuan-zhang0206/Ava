// Pure model of the cluster view: the curves' stacking, the lane tree and its folds, where a message
// edge runs, and which window to fetch for a viewport. No React, no I/O. The time axis itself
// (viewport, zoom, pan, ticks) is the run timeline's (`../run-timeline/timeline-model`).

import type {
  AgentLane,
  ClusterCurves,
  CurveBucket,
  MessageEdge,
} from "@/lib/contracts/types";
import type { Viewport } from "../run-timeline/timeline-model";

const MS = 1000;

// ---------- curves ----------

/** One agent's cost in one bucket, stacked on the ones below it; `y0`..`y1` in USD per minute. */
export interface CostSegment {
  agentId: number;
  /** The bucket's extent in epoch milliseconds. */
  x0: number;
  x1: number;
  y0: number;
  y1: number;
}

export interface BucketValue {
  x0: number;
  x1: number;
  value: number;
}

export interface QueuePoint {
  x0: number;
  x1: number;
  p50: number | null;
  p95: number | null;
  samples: number;
}

function bucketSpan(curves: ClusterCurves, bucket: CurveBucket): { x0: number; x1: number } {
  const x0 = Date.parse(bucket.ts);
  return { x0, x1: x0 + curves.bucket_seconds * MS };
}

/** Converts an amount per bucket to a per-minute rate. */
export function perMinute(amount: number, bucketSeconds: number): number {
  return (amount * 60) / bucketSeconds;
}

/**
 * Cost per minute, stacked by agent in `order` (the lane order, so a spawn tree stacks together;
 * agents the order does not name go on top, by id). Buckets without cost draw nothing.
 */
export function costSegments(
  curves: ClusterCurves,
  order: readonly number[],
): { segments: CostSegment[]; max: number } {
  const rank = new Map(order.map((id, index) => [id, index]));
  const segments: CostSegment[] = [];
  let max = 0;
  for (const bucket of curves.buckets) {
    const { x0, x1 } = bucketSpan(curves, bucket);
    const costs = bucket.costs
      .filter((entry) => entry.cost_usd > 0)
      .sort(
        (a, b) =>
          (rank.get(a.agent_id) ?? Number.MAX_SAFE_INTEGER) -
            (rank.get(b.agent_id) ?? Number.MAX_SAFE_INTEGER) || a.agent_id - b.agent_id,
      );
    let top = 0;
    for (const entry of costs) {
      const rate = perMinute(entry.cost_usd, curves.bucket_seconds);
      segments.push({ agentId: entry.agent_id, x0, x1, y0: top, y1: top + rate });
      top += rate;
    }
    max = Math.max(max, top);
  }
  return { segments, max };
}

/** Agents with at least one LLM call in each bucket. */
export function activeSeries(curves: ClusterCurves): BucketValue[] {
  return curves.buckets.map((bucket) => ({ ...bucketSpan(curves, bucket), value: bucket.active_agents }));
}

/** Agent-to-agent messages sent per minute in each bucket. */
export function messageSeries(curves: ClusterCurves): BucketValue[] {
  return curves.buckets
    .filter((bucket) => bucket.messages > 0)
    .map((bucket) => ({
      ...bucketSpan(curves, bucket),
      value: perMinute(bucket.messages, curves.bucket_seconds),
    }));
}

/** Queue time (claimed minus created) of the messages sent in each bucket that have been claimed. */
export function queueSeries(curves: ClusterCurves): QueuePoint[] {
  return curves.buckets
    .filter((bucket) => bucket.queue_samples > 0)
    .map((bucket) => ({
      ...bucketSpan(curves, bucket),
      p50: bucket.queue_p50_seconds,
      p95: bucket.queue_p95_seconds,
      samples: bucket.queue_samples,
    }));
}

/** The smallest 1, 2 or 5 times a power of ten that is at least `value` (1 for a non-positive value). */
export function niceMax(value: number): number {
  if (!(value > 0)) return 1;
  const exponent = Math.floor(Math.log10(value));
  const base = 10 ** exponent;
  const mantissa = value / base;
  const step = [1, 2, 5, 10].find((candidate) => mantissa <= candidate + 1e-9) ?? 10;
  return step * base;
}

/** A position on a plot: 0 at the bottom, `height` pixels at `max`. */
export function plotY(value: number, max: number, height: number): number {
  return height - (Math.min(Math.max(value, 0), max) / max) * height;
}

/** Where a time falls across a track of `trackPx` pixels showing `view`. */
export function timeX(ms: number, view: Viewport, trackPx: number): number {
  return ((ms - view.from) / (view.to - view.from)) * trackPx;
}

// ---------- formatting ----------

export function formatUsd(value: number): string {
  if (value === 0) return "$0";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  if (value < 100) return `$${value.toFixed(2)}`;
  return `$${Math.round(value)}`;
}

export function formatSeconds(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * MS)}ms`;
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, "0")}m`;
}

// ---------- the lane tree ----------

export interface LaneRow {
  lane: AgentLane;
  hasChildren: boolean;
  folded: boolean;
  /** Agents hidden under this lane while it is folded (all descendants). */
  hidden: number;
}

/** Every lane's parent, keyed by agent; a root maps to null. */
export function parentMap(lanes: readonly AgentLane[]): Map<number, number | null> {
  return new Map(lanes.map((lane) => [lane.agent_id, lane.parent]));
}

/**
 * The rows to draw: the lanes in tree order, minus those under a folded lane. A lane's
 * descendants follow it directly in `lanes` (tree order), so a fold skips the following lanes deeper
 * than the folded one.
 */
export function visibleRows(lanes: readonly AgentLane[], folded: ReadonlySet<number>): LaneRow[] {
  const rows: LaneRow[] = [];
  let skipBelow: number | null = null;
  lanes.forEach((lane, index) => {
    if (skipBelow !== null) {
      if (lane.depth > skipBelow) return;
      skipBelow = null;
    }
    const next = lanes.at(index + 1);
    const hasChildren = next !== undefined && next.depth > lane.depth;
    const isFolded = hasChildren && folded.has(lane.agent_id);
    let hidden = 0;
    if (isFolded) {
      for (let j = index + 1; j < lanes.length && lanes[j].depth > lane.depth; j += 1) hidden += 1;
      skipBelow = lane.depth;
    }
    rows.push({ lane, hasChildren, folded: isFolded, hidden });
  });
  return rows;
}

/** The lane an agent is drawn on: its own, or the nearest ancestor's when it sits under a fold. */
export function visibleLaneOf(
  agentId: number,
  rows: readonly LaneRow[],
  parents: ReadonlyMap<number, number | null>,
): number | null {
  const shown = new Set(rows.map((row) => row.lane.agent_id));
  let at: number | null | undefined = agentId;
  const seen = new Set<number>();
  while (at !== null && at !== undefined && !seen.has(at)) {
    if (shown.has(at)) return at;
    seen.add(at);
    at = parents.get(at);
  }
  return null;
}

/** A stable colour per agent from its place in the tree order (golden-angle hues stay apart). */
export function agentColors(lanes: readonly AgentLane[]): Map<number, string> {
  return new Map(
    lanes.map((lane, index) => [lane.agent_id, `hsl(${Math.round((index * 137.508) % 360)} 55% 52%)`]),
  );
}

// ---------- message edges ----------

export interface DrawnEdge {
  edge: MessageEdge;
  /** The lanes it runs between (an end under a fold is on its folded ancestor's lane). */
  from: number;
  to: number;
  /** Whether an end sits on a folded ancestor's lane rather than the agent's own. */
  aggregated: boolean;
}

/**
 * The edges to draw on `rows`: each end moved to the lane its agent is drawn on, dropping those
 * whose two ends land on the same lane (a message inside a folded subtree) or whose agent is not a lane.
 */
export function drawnEdges(
  edges: readonly MessageEdge[],
  rows: readonly LaneRow[],
  parents: ReadonlyMap<number, number | null>,
): DrawnEdge[] {
  const out: DrawnEdge[] = [];
  const shown = new Set(rows.map((row) => row.lane.agent_id));
  for (const edge of edges) {
    const from = visibleLaneOf(edge.sender, rows, parents);
    const to = visibleLaneOf(edge.receiver, rows, parents);
    if (from === null || to === null || from === to) continue;
    out.push({ edge, from, to, aggregated: !shown.has(edge.sender) || !shown.has(edge.receiver) });
  }
  return out;
}

/** Seconds the message waited from creation to the receiver's claim; null while unclaimed. */
export function edgeQueueSeconds(edge: MessageEdge): number | null {
  if (edge.read_at === null) return null;
  return Math.max((Date.parse(edge.read_at) - Date.parse(edge.sent_at)) / MS, 0);
}

/** The path of a connector from the sender's point to the receiver's, bowing sideways in time. */
export function edgePath(x0: number, y0: number, x1: number, y1: number): string {
  const bow = Math.max(Math.abs(x1 - x0) * 0.5, 12);
  return `M ${x0} ${y0} C ${x0 + bow} ${y0}, ${x1 - bow} ${y1}, ${x1} ${y1}`;
}

// ---------- the window to fetch ----------

/** A fetched window reaches this share of the view past each side of it, so a small pan needs no read. */
export const FETCH_MARGIN = 0.5;

/** The view widened by the margin and kept inside `base`. */
export function expandedWindow(view: Viewport, base: Viewport): Viewport {
  const margin = (view.to - view.from) * FETCH_MARGIN;
  return { from: Math.max(view.from - margin, base.from), to: Math.min(view.to + margin, base.to) };
}

/**
 * The window to hold data for: the current one while `view` lies inside it and it is not much wider
 * than a fresh one would be (zoomed in far), else a fresh one around `view`.
 */
export function nextFetchWindow(loaded: Viewport | null, view: Viewport, base: Viewport): Viewport {
  const fresh = expandedWindow(view, base);
  if (loaded === null) return fresh;
  const inside = view.from >= loaded.from && view.to <= loaded.to;
  const tooCoarse = loaded.to - loaded.from > 2 * (fresh.to - fresh.from);
  return inside && !tooCoarse ? loaded : fresh;
}

/** A window as the ISO strings of the read, whole seconds so equal windows share a cache entry. */
export function windowIso(window: Viewport): { from: string; to: string } {
  const second = (ms: number) => new Date(Math.floor(ms / MS) * MS).toISOString();
  return { from: second(window.from), to: second(Math.ceil(window.to / MS) * MS) };
}
