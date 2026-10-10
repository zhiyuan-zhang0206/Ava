import { describe, expect, it } from "vitest";

import type { RunTimelineLink, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import {
  distanceToCurve,
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

  it("lands any other event on the receiver's Lifecycle row", () => {
    const [one] = resolveLinks([link({ kind: "terminate" })], new Set([1, 2]), loaded);
    expect(one.to).toEqual({ row: "lifecycle", agent: 2, ms: Date.parse(iso(10)) });
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

describe("hitting a curve", () => {
  const down: Curve = { key: "a", x0: 100, y0: 0, x1: 100, y1: 200 };
  const slanted: Curve = { key: "b", x0: 300, y0: 0, x1: 500, y1: 200 };

  it("measures the distance to the curve, zero on it", () => {
    expect(distanceToCurve(down, 100, 80)).toBeCloseTo(0, 5);
    expect(distanceToCurve(down, 107, 80)).toBeCloseTo(7, 1);
  });

  it("hits the nearest curve within the tolerance and nothing beyond it", () => {
    expect(hitLink([down], 102, 100, 4)).toBe("a");
    expect(hitLink([down], 110, 100, 4)).toBeNull();
    expect(hitLink([down, slanted], 100, 100, 4)).toBe("a");
  });

  it("prefers the one drawn later when two are equally near", () => {
    expect(hitLink([down, { ...down, key: "c" }], 100, 100, 4)).toBe("c");
  });
});
