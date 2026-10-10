import { describe, expect, it } from "vitest";

import type { RunTimelineLink, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import {
  curveOf,
  curvePoint,
  distanceToCurve,
  endTangent,
  externalLinks,
  hitLink,
  nearestLink,
  nearestUnit,
  resolveLinks,
  stepLink,
  type Curve,
} from "./timeline-links";

const iso = (minute: number) => new Date(Date.UTC(2026, 9, 4, 12, minute)).toISOString();

const link = (partial: Partial<RunTimelineLink>): RunTimelineLink => ({
  kind: "send_message",
  ts: iso(10),
  sender: 1,
  receiver: 2,
  inbound_id: null,
  fork_from: null,
  preview: null,
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
  const row = externalLinks(resolved);

  it("holds only the links with an end outside the view, left to right", () => {
    expect(row.map((l) => l.from.ms)).toEqual([Date.parse(iso(10)), Date.parse(iso(30))]);
  });

  it("steps to the neighbour and stops at the ends", () => {
    expect(stepLink(row, row[0].key, "right")).toBe(row[1].key);
    expect(stepLink(row, row[0].key, "left")).toBeNull();
    expect(stepLink(row, row[1].key, "right")).toBeNull();
  });

  it("enters at the event nearest in time", () => {
    expect(nearestLink(row, Date.parse(iso(27)))).toBe(row[1].key);
    expect(nearestLink([], 0)).toBeNull();
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
