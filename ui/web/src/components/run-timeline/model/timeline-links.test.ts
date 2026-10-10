import { describe, expect, it } from "vitest";

import type { RunTimelineLink, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import {
  bucketOf,
  clusterArrows,
  clusterToMax,
  MAX_ARROWS,
  curveOf,
  curvePoint,
  distanceToCurve,
  endTangent,
  endIn,
  groupLinks,
  isHumanSource,
  hitLink,
  nearestLink,
  nearestUnit,
  resolveLinks,
  stepLink,
  type Curve,
} from "./timeline-links";

const iso = (minute: number) => new Date(Date.UTC(2026, 9, 4, 12, 0, minute * 60)).toISOString();

const link = (partial: Partial<RunTimelineLink>): RunTimelineLink => ({
  kind: "send_message",
  ts: iso(10),
  sender: 1,
  receiver: 2,
  inbound_id: null,
  fork_from: null,
  notice_id: null,
  ...partial,
});

const inbound = (inboundId: number | null, from: number, to: number): RunTimelineUnit =>
  ({ kind: "inbound", i0: 1, i1: 1, start: iso(from), end: iso(to), inbound_id: inboundId, source: "agent:1" }) as RunTimelineUnit;

const agent = (units: RunTimelineUnit[]) => ({ units }) as RunTimelineResponse;

describe("resolveLinks", () => {
  const loaded = new Map([
    [1, agent([])],
    [2, agent([inbound(7, 20, 22), inbound(null, 30, 31)])],
  ]);

  it("lands a message on the inbound block that carries its inbound id, the sender at the event's time", () => {
    const [one] = resolveLinks([link({ inbound_id: 7 })], new Set([1, 2]), loaded);
    expect(one.from).toEqual({ row: "units", agent: 1, ms: Date.parse(iso(10)) });
    expect(one.to).toEqual({ row: "units", agent: 2, ms: Date.parse(iso(21)) });
    expect(one.external).toBeNull();
    expect(one.unmatched).toBe(false);
  });

  it("falls back to the event's time in the Messages row when no block carries the id", () => {
    const [one] = resolveLinks([link({ inbound_id: 99 })], new Set([1, 2]), loaded);
    expect(one.to).toEqual({ row: "units", agent: 2, ms: Date.parse(iso(10)) });
    expect(one.unmatched).toBe(true);
  });

  it("lands any other event in the receiver's Messages row too: on its inbound block when it has one, else at the event's time", () => {
    const [on, at, spawn] = resolveLinks(
      [link({ kind: "terminate", inbound_id: 7 }), link({ kind: "terminate", inbound_id: 99 }), link({ kind: "spawn" })],
      new Set([1, 2]),
      loaded,
    );
    expect(on.to).toEqual({ row: "units", agent: 2, ms: Date.parse(iso(21)) });
    expect(at.to).toEqual({ row: "units", agent: 2, ms: Date.parse(iso(10)) });
    expect([on.unmatched, at.unmatched]).toEqual([false, true]);
    // An event that was not delivered as an inbound row has no block to be matched to, and says nothing about it.
    expect([spawn.to.row, spawn.unmatched]).toEqual(["units", false]);
  });

  it("puts an end that is not in the view on the Other agents row and names that agent", () => {
    const [out, into] = resolveLinks(
      [link({ kind: "spawn", receiver: 9 }), link({ sender: 8, receiver: 2 })],
      new Set([1, 2]),
      loaded,
    );
    expect(out.to.row).toBe("other");
    expect(out.external).toBe(9);
    expect(into.from).toEqual({ row: "other", agent: 8, ms: Date.parse(iso(10)) });
    expect(into.external).toBe(8);
  });

  it("leaves out a link with no end in the view and one whose agent has not loaded", () => {
    const links = [link({ sender: 8, receiver: 9 }), link({ sender: 1, receiver: 3 })];
    expect(resolveLinks(links, new Set([1, 2, 3]), loaded)).toEqual([]);
  });

  it("keeps the links apart by their position even when everything else is equal", () => {
    const keys = resolveLinks([link({}), link({})], new Set([1, 2]), loaded).map((l) => l.key);
    expect(new Set(keys).size).toBe(2);
  });
});

describe("the Other agents row", () => {
  const resolved = resolveLinks(
    [link({ ts: iso(30), receiver: 9 }), link({ ts: iso(10), sender: 8, receiver: 2 }), link({})],
    new Set([1, 2]),
    new Map([
      [1, agent([])],
      [2, agent([])],
    ]),
  );
  const row = groupLinks(resolved).other;

  it("holds only the links with an end outside the view, left to right", () => {
    expect(row.map((l) => endIn(l, "other")?.ms)).toEqual([Date.parse(iso(10)), Date.parse(iso(30))]);
  });

  it("steps to the neighbour and stops at the ends", () => {
    expect(stepLink(row, row[0].key, "right")).toBe(row[1].key);
    expect(stepLink(row, row[0].key, "left")).toBeNull();
    expect(stepLink(row, row[1].key, "right")).toBeNull();
  });

  it("enters at the event nearest in time", () => {
    expect(nearestLink(row, "other", Date.parse(iso(27)))).toBe(row[1].key);
    expect(nearestLink([], "other", 0)).toBeNull();
  });
});

describe("the User group", () => {
  const human = (i0: number, source: string, from: number, to: number, inboundId: number | null): RunTimelineUnit =>
    ({ ...inbound(inboundId, from, to), i0, i1: i0, source, preview: `from ${source}` });
  const loaded = new Map([[2, agent([human(1, "user", 20, 22, 5), human(2, "ui:page:fleet", 30, 31, null), human(3, "watcher:7", 40, 41, 9), inbound(8, 50, 51)])]]);

  it("takes a person's chat message from the receiver's own block, with no audit event, and ends it on that block", () => {
    const user = groupLinks(resolveLinks([], new Set([2]), loaded)).user;
    expect(user.map((l) => [l.link.kind, l.link.sender, l.link.receiver, l.userSource])).toEqual([
      ["send_message", null, 2, "user"],
      ["send_message", null, 2, "ui:page:fleet"],
    ]);
    expect(user[0].to).toEqual({ row: "units", agent: 2, ms: Date.parse(iso(21)) });
    expect(user[0].from).toEqual({ row: "user", agent: 0, ms: Date.parse(iso(20)) });
    expect([user[0].unmatched, user[0].external]).toEqual([false, null]);
    // A block with no inbound id (an old history) is still exact: it is the block itself.
    expect(user[1].to).toEqual({ row: "units", agent: 2, ms: Date.parse(iso(30.5)) });
  });

  it("leaves out the messages of agents, watchers, shells and the system", () => {
    expect(isHumanSource("agent:1")).toBe(false);
    expect(isHumanSource("watcher:7")).toBe(false);
    expect(isHumanSource("system")).toBe(false);
    expect(isHumanSource(null)).toBe(false);
    expect(groupLinks(resolveLinks([], new Set([2]), loaded)).user).toHaveLength(2);
  });

  it("draws the user's lifecycle events (no sender) and an agent's notice to the user (no receiver)", () => {
    const [spawn, notice] = resolveLinks(
      [link({ kind: "spawn", sender: null, receiver: 2 }), link({ kind: "notice", sender: 2, receiver: null, ts: iso(15) })],
      new Set([2]),
      new Map([[2, agent([])]]),
    );
    expect(spawn.from.row).toBe("user");
    expect(spawn.to).toEqual({ row: "units", agent: 2, ms: Date.parse(iso(10)) });
    expect(notice.from).toEqual({ row: "units", agent: 2, ms: Date.parse(iso(15)) });
    expect(notice.to).toEqual({ row: "user", agent: 0, ms: Date.parse(iso(15)) });
    expect([spawn.external, notice.external]).toEqual([null, null]);
  });

  it("leaves out the user's event to an agent that is not in the view", () => {
    expect(resolveLinks([link({ kind: "spawn", sender: null, receiver: 9 })], new Set([2]), new Map([[2, agent([])]]))).toEqual([]);
  });

  it("keeps the two groups apart and each left to right", () => {
    const all = resolveLinks([link({ kind: "spawn", sender: 8, receiver: 2, ts: iso(5) })], new Set([2]), loaded);
    const grouped = groupLinks(all);
    expect(grouped.user).toHaveLength(2);
    expect(grouped.other).toHaveLength(1);
    expect(grouped.user.map((l) => l.from.ms)).toEqual([...grouped.user.map((l) => l.from.ms)].sort((a, b) => a - b));
  });
});

describe("nearestUnit", () => {
  it("is the block whose middle is nearest in time", () => {
    const data = agent([inbound(1, 0, 2), inbound(2, 20, 22)]);
    expect(nearestUnit(data, Date.parse(iso(18)))).toMatchObject({ kind: "unit", unitKind: "inbound" });
    expect(nearestUnit(agent([]), 0)).toBeNull();
  });
});

describe("the curve", () => {
  it("bends visibly even when both ends are at the same x, within bounds", () => {
    const c = curveOf("k", 100, 0, 100, 200);
    expect(Math.abs(c.c1x - c.x0)).toBeGreaterThanOrEqual(20);
    expect(Math.abs(c.c1x - c.x0)).toBeLessThanOrEqual(140);
    expect(c.c1x - c.x0).toBe(c.c2x - c.x1);
    const short = curveOf("k", 100, 0, 100, 4);
    expect(Math.abs(short.c1x - short.x0)).toBeGreaterThanOrEqual(20);
  });

  it("pulls further for longer vertical distances, up to a bound", () => {
    const pull = (dy: number) => Math.abs(curveOf("k", 0, 0, 0, dy).c1x);
    expect(pull(400)).toBeGreaterThan(pull(100));
    expect(pull(10000)).toBeLessThanOrEqual(140);
  });

  it("varies the size of the bend between links, never its side", () => {
    const keys = Array.from({ length: 60 }, (_, i) => `link-${i}`);
    const pulls = keys.map((k) => curveOf(k, 0, 0, 0, 200).c1x);
    expect(new Set(pulls).size).toBeGreaterThan(1);
    expect(pulls.every((pull) => pull > 0)).toBe(true);
  });

  it("bulges every curve to the right of the line between its ends, up or down", () => {
    const cases: [number, number, number, number][] = [
      [100, 0, 100, 200],
      [100, 200, 100, 0],
      [100, 0, 400, 300],
      [400, 300, 100, 0],
      [100, 0, 100, 4],
      [100, 0, 100, 5000],
    ];
    for (const [i, [x0, y0, x1, y1]] of cases.entries()) {
      const c = curveOf(`k${i}`, x0, y0, x1, y1);
      for (const [cx, cy] of [[c.c1x, c.c1y], [c.c2x, c.c2y]]) {
        const chordX = x0 + ((cy - y0) / (y1 - y0)) * (x1 - x0);
        expect(cx).toBeGreaterThan(chordX);
      }
    }
  });

  it("ends tangent to the curve, not along the line between the ends", () => {
    const c = curveOf("k", 0, 0, 40, 400);
    const tan = endTangent(c);
    expect(Math.hypot(tan.x, tan.y)).toBeCloseTo(1, 5);
    // The end control point is level with the end and to its right: the head points left, not down the chord.
    expect(tan.y).toBeCloseTo(0, 5);
    expect(tan.x).toBeLessThan(-0.9);
  });
});

describe("hitting a curve", () => {
  const down: Curve = curveOf("a", 100, 0, 100, 200);
  const slanted: Curve = curveOf("b", 600, 0, 800, 200);

  it("measures the distance to the curve, zero on it", () => {
    const mid = curvePoint(down, 0.3);
    expect(distanceToCurve(down, mid.x, mid.y)).toBeLessThan(0.5);
    expect(distanceToCurve(down, mid.x + 40, mid.y)).toBeGreaterThan(10);
  });

  it("hits the nearest curve within the tolerance and nothing beyond it", () => {
    const p = curvePoint(down, 0.5);
    expect(hitLink([down], p.x + 2, p.y, 4)).toBe("a");
    expect(hitLink([down], p.x + 30, p.y, 4)).toBeNull();
    expect(hitLink([down, slanted], p.x, p.y, 4)).toBe("a");
  });

  it("prefers the one drawn later when two are equally near", () => {
    const p = curvePoint(down, 0.5);
    expect(hitLink([down, { ...down, key: "c" }], p.x, p.y, 4)).toBe("c");
  });
});

describe("merging arrows that lie close together", () => {
  const arrow = (key: string, x0: number, x1: number, bucket = "a") => ({ key, bucket, x0, x1 });
  const sizes = (arrows: ReturnType<typeof arrow>[], px: number) =>
    clusterArrows(arrows, px)
      .map((c) => c.members.length)
      .sort((a, b) => b - a);
  const PX = 14;

  it("merges arrows whose two ends are both closer than the distance, and puts the merged one in the middle of them", () => {
    const [one, ...rest] = clusterArrows([arrow("p", 100, 300), arrow("q", 104, 306), arrow("r", 108, 303)], PX);
    expect(rest).toEqual([]);
    expect(one.members).toEqual(["p", "q", "r"]);
    expect(one.key).toBe("group:p:3");
    expect(one.x0).toBeCloseTo(104, 5);
    expect(one.x1).toBeCloseTo(303, 5);
  });

  it("leaves an arrow alone under its own key", () => {
    expect(clusterArrows([arrow("only", 10, 20)], PX)).toEqual([{ key: "only", bucket: "a", members: ["only"], x0: 10, x1: 20 }]);
  });

  it("keeps arrows apart when either end is farther than the distance", () => {
    expect(sizes([arrow("p", 100, 300), arrow("q", 100 + PX, 300)], PX)).toEqual([1, 1]);
    expect(sizes([arrow("p", 100, 300), arrow("q", 100, 300 + PX)], PX)).toEqual([1, 1]);
    expect(sizes([arrow("p", 100, 300), arrow("q", 100 + PX - 1, 300 + PX - 1)], PX)).toEqual([2]);
  });

  it("never merges arrows of different kinds or between different rows", () => {
    const kinds = ["send_message", "spawn"].map((k) => `${k}|units:1|units:2`);
    expect(sizes([arrow("p", 100, 300, kinds[0]), arrow("q", 101, 301, kinds[1])], 1000)).toEqual([1, 1]);
    const rows = ["send_message|units:1|units:2", "send_message|units:1|units:3"];
    expect(sizes([arrow("p", 100, 300, rows[0]), arrow("q", 101, 301, rows[1])], 1000)).toEqual([1, 1]);
  });

  it("puts the links with different peers outside the view in one bucket, since they share the Other agents row", () => {
    const [a, b] = resolveLinks(
      [link({ sender: 1, receiver: 8 }), link({ sender: 1, receiver: 9 })],
      new Set([1, 2]),
      new Map([[1, agent([])], [2, agent([])]]),
    );
    expect(bucketOf(a)).toBe(bucketOf(b));
  });

  it("buckets a link by its kind and the two rows its ends stand in", () => {
    const [a, b, c] = resolveLinks(
      [link({}), link({ kind: "spawn" }), link({ receiver: 3 })],
      new Set([1, 2, 3]),
      new Map([[1, agent([])], [2, agent([])], [3, agent([])]]),
    );
    expect(new Set([bucketOf(a), bucketOf(b), bucketOf(c)]).size).toBe(3);
  });

  it("counts every member exactly once, however they chain", () => {
    const many = Array.from({ length: 500 }, (_, i) => arrow(`k${i}`, (i * 7) % 200, (i * 13) % 400, `b${i % 3}`));
    const clusters = clusterArrows(many, PX);
    expect(clusters.reduce((n, c) => n + c.members.length, 0)).toBe(500);
    expect(new Set(clusters.flatMap((c) => c.members)).size).toBe(500);
  });
});

describe("merging down to a limit", () => {
  const spread = (n: number, scale: number, bucket = "a") =>
    Array.from({ length: n }, (_, i) => ({ key: `k${i}`, bucket, x0: ((i * 37) % 997) * scale, x1: ((i * 91) % 991) * scale }));
  const total = (c: { clusters: { members: string[] }[] }) => c.clusters.reduce((n, x) => n + x.members.length, 0);

  it("merges nothing while the arrows already fit", () => {
    const found = clusterToMax(spread(20, 1), 30, 2000);
    expect(found.clusters).toHaveLength(20);
    expect(found.px).toBe(0);
    expect(found.withinMax).toBe(true);
  });

  it("keeps at most the limit, with every arrow in exactly one cluster, by the smallest distance that does", () => {
    const arrows = spread(400, 1);
    for (const max of [20, 30, 50]) {
      const found = clusterToMax(arrows, max, 2000);
      expect(found.clusters.length).toBeLessThanOrEqual(max);
      expect(total(found)).toBe(400);
      expect(found.withinMax).toBe(true);
      // A smaller distance would have left more than the limit.
      expect(clusterArrows(arrows, found.px - 1).length).toBeGreaterThan(max);
    }
  });

  it("merges less the more room there is: a larger limit needs a smaller distance", () => {
    const arrows = spread(400, 1);
    expect(clusterToMax(arrows, 50, 2000).px).toBeLessThan(clusterToMax(arrows, 20, 2000).px);
  });

  it("falls apart on zooming in: fewer arrows are in view, so the distance shrinks, and with few enough there is none", () => {
    // 400 arrows over the whole window; zoomed in 8x only an eighth of them are on screen, spread 8x wider.
    const whole = Array.from({ length: 400 }, (_, i) => ({ key: `k${i}`, bucket: "a", x0: (i * 2.5) % 1000, x1: (i * 2.5 + 40) % 1000 }));
    const zoomed = whole.filter((a) => a.x0 < 125).map((a) => ({ ...a, x0: a.x0 * 8, x1: a.x1 * 8 }));
    const far = clusterToMax(whole, 30, 2000);
    const near = clusterToMax(zoomed, 30, 2000);
    expect(near.px).toBeLessThan(far.px);
    expect(clusterToMax(zoomed.slice(0, 20), 30, 2000).clusters).toHaveLength(20);
  });

  it("does not merge across kinds or rows to reach the limit, and says so", () => {
    const arrows = ["a", "b", "c", "d"].flatMap((bucket) => spread(10, 1, bucket));
    const found = clusterToMax(arrows, 3, 2000);
    expect(found.withinMax).toBe(false);
    expect(found.clusters).toHaveLength(4);
    for (const c of found.clusters) expect(new Set(c.members.map((m) => m)).size).toBe(c.members.length);
    expect(new Set(found.clusters.map((c) => c.bucket)).size).toBe(4);
  });

  it("has a sensible default limit", () => {
    expect(MAX_ARROWS).toBeGreaterThanOrEqual(10);
  });

  it("stays fast on thousands of arrows, the search included", () => {
    for (const n of [5000, 20000]) {
      const arrows = Array.from({ length: n }, (_, i) => ({ key: `k${i}`, bucket: `b${i % 8}`, x0: (i * 37) % 1800, x1: (i * 91) % 1800 }));
      const started = performance.now();
      clusterToMax(arrows, 30, 3600);
      // Measured in the report; the bound is generous so a slow CI machine does not flake.
      expect(performance.now() - started).toBeLessThan(2000);
    }
  });
});
