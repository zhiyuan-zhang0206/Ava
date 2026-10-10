// Pure model of the arrows between agents: which end of an agent-to-agent event lands where, the
// curve between two points and which curve a pointer is on. No React, no canvas, no I/O.

import type { RunTimelineLink, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import type { Selection } from "./timeline-model";

/** What an arrow is: an event of the record, or (`user_message`) a chat message of the user, which has no event and is told apart from the messages between agents. */
export type LinkKind = RunTimelineLink["kind"] | "user_message";

/** The kinds in legend order; a kind is told apart by its color alone. */
export const LINK_KINDS: readonly LinkKind[] = ["send_message", "user_message", "spawn", "fork", "terminate", "restart", "resurrect", "notice"];

export const LINK_COLORS: Record<LinkKind, string> = {
  send_message: "#3b82f6",
  user_message: "#92400e",
  spawn: "#22c55e",
  fork: "#14b8a6",
  terminate: "#ef4444",
  restart: "#f59e0b",
  resurrect: "#a855f7",
  notice: "#ec4899",
};

/** The rows an arrow can end in: an agent's Messages row, or the User or Other agents group (`agent` is then the peer, 0 for the user). */
export type LinkRow = "units" | "user" | "other";

export interface LinkEnd {
  row: LinkRow;
  agent: number;
  /** Epoch milliseconds: where on the shared axis. */
  ms: number;
}

export interface ResolvedLink {
  /** Stable across redraws: kind, time, ends and position in the response. */
  key: string;
  /** What it is for the legend, colors and merging; `link.kind` is the record's (a user's message has no record). */
  kind: LinkKind;
  link: RunTimelineLink;
  from: LinkEnd;
  to: LinkEnd;
  /** The agent at the end that is not in the view, when one is (the user is no agent). */
  external: number | null;
  /** The receiver's block the event ends on, when it ends on one: its message is what the details show, as for a selected block. */
  block: RunTimelineUnit | null;
  /** The user's source (`user`, `ui:page:...`), when the user is an end. */
  userSource: string | null;
  /** The event names an inbound row of the receiver in view but no block carries it: the arrow ends at the event's time, not on a block. */
  unmatched: boolean;
}

const middle = (unit: Pick<RunTimelineUnit, "start" | "end">) => (Date.parse(unit.start) + Date.parse(unit.end)) / 2;

/** Whether an inbound source is a person (the user's own message), not an agent, a watcher, a shell or the system. */
export const isHumanSource = (source: string | null) => source !== null && (source === "user" || source.startsWith("ui:page:"));

/**
 * The ends of each link. Both ends of an in-view agent are in its Messages row. The sender stands at
 * the event's time (the block it was working on is not guessed). The receiver is the block of the
 * inbound row the event was delivered as (matched by `inbound_id`), else the event's time. An end
 * whose agent is not in the view lands on the Other agents row at the event's time; the user (no
 * agent) lands on the User row. A link whose in-view agent has not loaded yet is left out until it
 * has.
 *
 * The user's chat messages have no audit event: they are read from the inbound blocks whose source is
 * a person, each ending on its own block and starting at the block's time (the time the agent read
 * it; the time the user sent it is not on the block).
 */
export function resolveLinks(
  links: readonly RunTimelineLink[],
  inView: ReadonlySet<number>,
  loaded: ReadonlyMap<number, RunTimelineResponse>,
): ResolvedLink[] {
  const out: ResolvedLink[] = [];
  links.forEach((link, index) => {
    const ms = Date.parse(link.ts);
    const { sender, receiver } = link;
    const senderIn = sender !== null && inView.has(sender);
    const receiverIn = receiver !== null && inView.has(receiver);
    if (!senderIn && !receiverIn) return;
    if ((senderIn && !loaded.has(sender)) || (receiverIn && !loaded.has(receiver))) return;
    const userEnd = sender === null || receiver === null;
    const outer = (agent: number | null): LinkEnd => (agent === null ? { row: "user", agent: 0, ms } : { row: "other", agent, ms });
    const from: LinkEnd = senderIn ? { row: "units", agent: sender, ms } : outer(sender);
    let to: LinkEnd = outer(receiver);
    let unmatched = false;
    let block: RunTimelineUnit | null = null;
    if (receiverIn) {
      block =
        link.inbound_id === null ? null : (loaded.get(receiver)?.units.find((unit) => unit.inbound_id === link.inbound_id) ?? null);
      unmatched = link.inbound_id !== null && block === null;
      to = { row: "units", agent: receiver, ms: block === null ? ms : middle(block) };
    }
    out.push({
      kind: link.kind,
      key: `${link.kind}-${link.ts}-${link.sender ?? "user"}-${link.receiver ?? "user"}-${index}`,
      link,
      from,
      to,
      external: userEnd || (senderIn && receiverIn) ? null : senderIn ? receiver : sender,
      block,
      userSource: userEnd ? "user" : null,
      unmatched,
    });
  });
  for (const [id, data] of loaded) {
    if (!inView.has(id)) continue;
    for (const unit of data.units) {
      if (unit.kind !== "inbound" || !isHumanSource(unit.source)) continue;
      const ms = Date.parse(unit.start);
      out.push({
        key: `user-${id}-${unit.i0}`,
        kind: "user_message",
        link: { kind: "send_message", ts: unit.start, sender: null, receiver: id, inbound_id: unit.inbound_id, fork_from: null, notice_id: null },
        from: { row: "user", agent: 0, ms },
        to: { row: "units", agent: id, ms: middle(unit) },
        external: null,
        block: unit,
        userSource: unit.source,
        unmatched: false,
      });
    }
  }
  return out;
}

/** The end of a link that stands in `row`, when one does. */
export const endIn = (l: ResolvedLink, row: LinkRow): LinkEnd | undefined => (l.from.row === row ? l.from : l.to.row === row ? l.to : undefined);

/** The links of the User group and of the Other agents group, each left to right. */
export function groupLinks(links: readonly ResolvedLink[]): { user: ResolvedLink[]; other: ResolvedLink[] } {
  const side = (row: LinkRow) =>
    links.filter((l) => endIn(l, row) !== undefined).sort((a, b) => (endIn(a, row)?.ms ?? 0) - (endIn(b, row)?.ms ?? 0));
  return { user: side("user"), other: side("other") };
}

/** The key of the neighbour of `current` in `row` (sorted by time), or null past either end. */
export function stepLink(row: readonly ResolvedLink[], current: string, dir: "left" | "right"): string | null {
  const at = row.findIndex((l) => l.key === current);
  if (at < 0) return null;
  const to = at + (dir === "left" ? -1 : 1);
  return to < 0 ? null : (row.at(to)?.key ?? null);
}

/** The link of `row` nearest in time to `ms`. */
export function nearestLink(links: readonly ResolvedLink[], row: LinkRow, ms: number): string | null {
  let best: { key: string; gap: number } | null = null;
  for (const l of links) {
    const gap = Math.abs((endIn(l, row)?.ms ?? Infinity) - ms);
    if (best === null || gap < best.gap) best = { key: l.key, gap };
  }
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

const BEND_MIN_PX = 24;
const BEND_MAX_PX = 60;
const BEND_PER_DY = 0.45;
const STAGGER_STEPS = 5;

/** A stable factor per link in 0.76..1.24, so near-simultaneous curves do not lie on one another. Always positive: it changes the size of the bend, never its side. */
function stagger(key: string): number {
  let h = 0;
  for (let i = 0; i < key.length; i += 1) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return 0.76 + (h % STAGGER_STEPS) * 0.12;
}

/**
 * The curve of a link: a C, bulging to the right (later in time) whatever the direction of the link,
 * so every arrow leans the same way. Both control points sit level with their end, pushed right by a
 * pull that grows with the vertical distance (bounded), so a straight-down link still bends visibly.
 */
export function curveOf(key: string, x0: number, y0: number, x1: number, y1: number): Curve {
  const base = Math.min(Math.max(Math.abs(y1 - y0) * BEND_PER_DY, BEND_MIN_PX), BEND_MAX_PX);
  const pull = base * stagger(key);
  return { key, x0, y0, c1x: x0 + pull, c1y: y0, c2x: x1 + pull, c2y: y1, x1, y1 };
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

/** The most arrows the panel shows at once; the merge distance adapts to the viewport to stay within it. */
export const ARROW_LIMITS = [20, 50] as const;
export const MAX_ARROWS: number = ARROW_LIMITS[0];

/** What clustering needs of an arrow on screen: its identity, which arrows it may merge with, and where its ends are. */
export interface Arrow {
  key: string;
  /** Arrows merge only within a bucket: the same kind between the same two rows (of the same agents). */
  bucket: string;
  x0: number;
  x1: number;
}

/** One drawn arrow: a single link, or several merged. */
export interface Cluster {
  /** The link's own key for one link; `group:<first key>:<count>` for several. */
  key: string;
  bucket: string;
  /** The members' keys, left to right by the start. */
  members: string[];
  /** The middle of the members' ends. */
  x0: number;
  x1: number;
}

/** The row an end stands in: an agent's own Messages row, or the User / Other agents group row (shared by every peer in it). */
const rowId = (end: LinkEnd) => (end.row === "units" ? `units:${end.agent}` : end.row);

/** The bucket of a link: its kind and the two rows its ends stand in. Arrows merge only within one. */
export const bucketOf = (l: ResolvedLink): string => `${l.kind}|${rowId(l.from)}|${rowId(l.to)}`;

/** Arrows grouped by bucket and sorted by their start, ready to be merged at any distance. */
export type PreparedArrows = readonly { bucket: string; list: readonly Arrow[] }[];

export function prepareArrows(arrows: readonly Arrow[]): PreparedArrows {
  const buckets = new Map<string, Arrow[]>();
  for (const a of arrows) {
    const list = buckets.get(a.bucket);
    if (list === undefined) buckets.set(a.bucket, [a]);
    else list.push(a);
  }
  return [...buckets].map(([bucket, list]) => ({ bucket, list: list.sort((p, q) => p.x0 - q.x0) }));
}

interface Open {
  first: Arrow;
  members: Arrow[];
}

/**
 * Merges the prepared arrows at distance `px`: within a bucket, in order of start, an arrow joins the
 * first open group whose first member is within `px` at both ends, else opens a group of its own (a
 * group is open only while a later start can still be within `px` of its first). With `emit` the
 * clusters are built; without it only their number is returned (what the search for `px` needs).
 */
function mergeAt(prepared: PreparedArrows, px: number, emit: ((c: Cluster) => void) | null): number {
  let count = 0;
  for (const { bucket, list } of prepared) {
    const close = (group: Open) => {
      count += 1;
      if (emit === null) return;
      const n = group.members.length;
      const keys = group.members.map((m) => m.key);
      emit({
        key: n === 1 ? keys[0] : `group:${keys[0]}:${n}`,
        bucket,
        members: keys,
        x0: group.members.reduce((sum, m) => sum + m.x0, 0) / n,
        x1: group.members.reduce((sum, m) => sum + m.x1, 0) / n,
      });
    };
    let open: Open[] = [];
    for (const a of list) {
      const stillOpen: Open[] = [];
      let joined = false;
      for (const group of open) {
        if (a.x0 - group.first.x0 >= px) {
          close(group);
          continue;
        }
        if (!joined && Math.abs(a.x1 - group.first.x1) < px) {
          group.members.push(a);
          joined = true;
        }
        stillOpen.push(group);
      }
      if (!joined) stillOpen.push({ first: a, members: [a] });
      open = stillOpen;
    }
    open.forEach(close);
  }
  return count;
}

/** The merge result at a fixed distance. */
export function clusterArrows(arrows: readonly Arrow[], px: number): Cluster[] {
  const out: Cluster[] = [];
  mergeAt(prepareArrows(arrows), px, (c) => out.push(c));
  return out;
}

const SEARCH_PRECISION_PX = 0.5;

/** What `clusterToMax` found: the clusters, the distance that gave them, and whether the limit could be kept. */
export interface Clustered {
  clusters: Cluster[];
  px: number;
  /** False when even merging everything the buckets allow leaves more than `max` arrows (arrows of different kinds or rows are never merged). */
  withinMax: boolean;
}

/**
 * The fewest merges that keep at most `max` arrows on screen: a binary search on the merge distance
 * for the smallest one whose result has at most `max` clusters. Merging is only by bucket, so if the
 * buckets alone outnumber `max` the result is the full merge and `withinMax` is false. The arrows are
 * sorted once; each step of the search only counts.
 */
export function clusterToMax(arrows: readonly Arrow[], max: number, widest: number): Clustered {
  const prepared = prepareArrows(arrows);
  const out: Cluster[] = [];
  const done = (px: number, withinMax: boolean): Clustered => {
    out.length = 0;
    mergeAt(prepared, px, (c) => out.push(c));
    return { clusters: [...out], px, withinMax };
  };
  if (mergeAt(prepared, 0, null) <= max) return done(0, true);
  if (mergeAt(prepared, widest, null) > max) return done(widest, false);
  let lo = 0;
  let hi = widest;
  while (hi - lo > SEARCH_PRECISION_PX) {
    const mid = (lo + hi) / 2;
    if (mergeAt(prepared, mid, null) <= max) hi = mid;
    else lo = mid;
  }
  return done(hi, true);
}
