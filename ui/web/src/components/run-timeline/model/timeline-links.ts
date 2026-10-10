// Pure model of the arrows between agents: which end of an agent-to-agent event lands where, the
// curve between two points and which curve a pointer is on. No React, no canvas, no I/O.

import type { RunTimelineLink, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import type { Selection } from "./timeline-model";

export type LinkKind = RunTimelineLink["kind"];

/** The kinds in legend order; a kind is told apart by its color alone. */
export const LINK_KINDS: readonly LinkKind[] = ["send_message", "spawn", "fork", "terminate", "restart", "resurrect"];

export const LINK_COLORS: Record<LinkKind, string> = {
  send_message: "#3b82f6",
  spawn: "#22c55e",
  fork: "#14b8a6",
  terminate: "#ef4444",
  restart: "#f59e0b",
  resurrect: "#a855f7",
};

/** Where an end sits: a row of an agent in the view, or the Other agents row (`agent` is then the peer). */
export interface LinkEnd {
  row: "units" | "lifecycle" | "other";
  agent: number;
  /** Epoch milliseconds: where on the shared axis. */
  ms: number;
}

export interface ResolvedLink {
  /** Stable across redraws: kind, time, ends and position in the response. */
  key: string;
  link: RunTimelineLink;
  from: LinkEnd;
  to: LinkEnd;
  /** The end that is not in the view, when one is. */
  external: number | null;
  /** A message whose receiver is in view but whose inbound block was not found: it ends at the event's time, not on a block. */
  unmatched: boolean;
}

const middle = (unit: Pick<RunTimelineUnit, "start" | "end">) => (Date.parse(unit.start) + Date.parse(unit.end)) / 2;

/**
 * The ends of each link. The sender stands in its Messages row at the event's time (the block it was
 * working on is not guessed). The receiver is, for a message, the inbound block it became (matched by
 * `inbound_id`; the Messages row at the event's time when no block carries it), for any other event
 * its Lifecycle row. An end whose agent is not in the view lands on the Other agents row at the
 * event's time. A link whose in-view agent has not loaded yet is left out until it has.
 */
export function resolveLinks(
  links: readonly RunTimelineLink[],
  inView: ReadonlySet<number>,
  loaded: ReadonlyMap<number, RunTimelineResponse>,
): ResolvedLink[] {
  const out: ResolvedLink[] = [];
  links.forEach((link, index) => {
    const ms = Date.parse(link.ts);
    const senderIn = inView.has(link.sender);
    const receiverIn = inView.has(link.receiver);
    if (!senderIn && !receiverIn) return;
    if ((senderIn && !loaded.has(link.sender)) || (receiverIn && !loaded.has(link.receiver))) return;
    const from: LinkEnd = senderIn ? { row: "units", agent: link.sender, ms } : { row: "other", agent: link.sender, ms };
    let to: LinkEnd = { row: "other", agent: link.receiver, ms };
    let unmatched = false;
    if (receiverIn) {
      if (link.kind === "send_message") {
        const block =
          link.inbound_id === null
            ? undefined
            : loaded.get(link.receiver)?.units.find((unit) => unit.kind === "inbound" && unit.inbound_id === link.inbound_id);
        unmatched = block === undefined;
        to = { row: "units", agent: link.receiver, ms: block === undefined ? ms : middle(block) };
      } else to = { row: "lifecycle", agent: link.receiver, ms };
    }
    out.push({
      key: `${link.kind}-${link.ts}-${link.sender}-${link.receiver}-${index}`,
      link,
      from,
      to,
      external: senderIn && receiverIn ? null : senderIn ? link.receiver : link.sender,
      unmatched,
    });
  });
  return out;
}

/** The links on the Other agents row, left to right. */
export function externalLinks(links: readonly ResolvedLink[]): ResolvedLink[] {
  return links.filter((l) => l.external !== null).sort((a, b) => a.from.ms - b.from.ms);
}

/** The key of the neighbour of `current` in `row` (sorted by time), or null past either end. */
export function stepLink(row: readonly ResolvedLink[], current: string, dir: "left" | "right"): string | null {
  const at = row.findIndex((l) => l.key === current);
  if (at < 0) return null;
  const to = at + (dir === "left" ? -1 : 1);
  return to < 0 ? null : (row.at(to)?.key ?? null);
}

/** The link of `row` nearest in time to `ms`. */
export function nearestLink(row: readonly ResolvedLink[], ms: number): string | null {
  let best: ResolvedLink | null = null;
  for (const l of row) if (best === null || Math.abs(l.from.ms - ms) < Math.abs(best.from.ms - ms)) best = l;
  return best?.key ?? null;
}

/** A cubic Bezier between two points; its control points sit level with the ends, pulled sideways. */
export interface Curve {
  key: string;
  x0: number;
  y0: number;
  c1x: number;
  c1y: number;
  c2x: number;
  c2y: number;
  x1: number;
  y1: number;
}

const BEND_MIN_PX = 28;
const BEND_MAX_PX = 140;
const BEND_PER_DY = 0.45;

/** A small stable offset per link (0..4), so near-simultaneous curves do not lie on one another. */
function stagger(key: string): number {
  let h = 0;
  for (let i = 0; i < key.length; i += 1) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return h % 5;
}

/**
 * The curve of a link: like a flow-chart connector, leaving and arriving sideways in an S. The pull
 * grows with the vertical distance between the ends (bounded), so a straight-down link still bends
 * visibly; the stagger varies it by up to +-24% per link.
 */
export function curveOf(key: string, x0: number, y0: number, x1: number, y1: number): Curve {
  const base = Math.min(Math.max(Math.abs(y1 - y0) * BEND_PER_DY, BEND_MIN_PX), BEND_MAX_PX);
  const pull = base * (0.76 + stagger(key) * 0.12) * (x1 >= x0 ? 1 : -1);
  return { key, x0, y0, c1x: x0 + pull, c1y: y0, c2x: x1 - pull, c2y: y1, x1, y1 };
}

export function curvePoint(c: Curve, t: number): { x: number; y: number } {
  const u = 1 - t;
  const w0 = u ** 3;
  const w1 = 3 * u * u * t;
  const w2 = 3 * u * t * t;
  const w3 = t ** 3;
  return { x: w0 * c.x0 + w1 * c.c1x + w2 * c.c2x + w3 * c.x1, y: w0 * c.y0 + w1 * c.c1y + w2 * c.c2y + w3 * c.y1 };
}

/** The unit direction of the curve at its end: where an arrowhead points. */
export function endTangent(c: Curve): { x: number; y: number } {
  const dx = c.x1 - c.c2x;
  const dy = c.y1 - c.c2y;
  const len = Math.hypot(dx, dy);
  return len === 0 ? { x: 0, y: 1 } : { x: dx / len, y: dy / len };
}

const SAMPLES = 24;

function segmentDistance(px: number, py: number, a: { x: number; y: number }, b: { x: number; y: number }): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  const t = len2 === 0 ? 0 : Math.min(Math.max(((px - a.x) * dx + (py - a.y) * dy) / len2, 0), 1);
  return Math.hypot(px - (a.x + t * dx), py - (a.y + t * dy));
}

/** The distance in pixels from a point to a curve (sampled as a polyline). */
export function distanceToCurve(c: Curve, px: number, py: number): number {
  let best = Infinity;
  let prev = curvePoint(c, 0);
  for (let i = 1; i <= SAMPLES; i += 1) {
    const next = curvePoint(c, i / SAMPLES);
    best = Math.min(best, segmentDistance(px, py, prev, next));
    prev = next;
  }
  return best;
}

/** The key of the curve nearest to the pointer within `tolerance` pixels (the later-drawn one on a tie), or null. */
export function hitLink(curves: readonly Curve[], px: number, py: number, tolerance: number): string | null {
  let key: string | null = null;
  let nearest = tolerance;
  for (const c of curves) {
    const d = distanceToCurve(c, px, py);
    if (d <= nearest) {
      nearest = d;
      key = c.key;
    }
  }
  return key;
}

/** When a selection of an agent starts, in epoch milliseconds (null when it is not in the data). */
export function selectionMs(data: RunTimelineResponse, selection: Selection): number | null {
  if (selection.kind === "node") {
    const node = data.nodes.find((candidate) => candidate.id === selection.id);
    return node === undefined ? null : Date.parse(node.start);
  }
  const unit = data.units.find((u) => u.i0 === selection.i0 && u.i1 === selection.i1 && u.kind === selection.unitKind);
  return unit === undefined ? null : Date.parse(unit.start);
}

/** The block of the Messages row nearest in time to `ms`, as a selection. */
export function nearestUnit(data: RunTimelineResponse, ms: number): Selection | null {
  let best: RunTimelineUnit | null = null;
  let gap = Infinity;
  for (const unit of data.units) {
    const d = Math.abs(middle(unit) - ms);
    if (d < gap) {
      gap = d;
      best = unit;
    }
  }
  return best === null ? null : { kind: "unit", i0: best.i0, i1: best.i1, unitKind: best.kind };
}
