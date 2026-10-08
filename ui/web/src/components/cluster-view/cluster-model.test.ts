import { describe, expect, it } from "vitest";

import type { AgentLane, ClusterCurves, MessageEdge } from "@/lib/contracts/types";

import {
  activeSeries,
  agentColors,
  costSegments,
  drawnEdges,
  edgePath,
  edgeQueueSeconds,
  expandedWindow,
  formatSeconds,
  formatUsd,
  messageSeries,
  nextFetchWindow,
  niceMax,
  parentMap,
  plotY,
  queueSeries,
  timeX,
  visibleLaneOf,
  visibleRows,
  windowIso,
} from "./cluster-model";

const T0 = Date.parse("2026-10-04T12:00:00Z");
const iso = (minutes: number) => new Date(T0 + minutes * 60_000).toISOString();

function curves(buckets: ClusterCurves["buckets"], bucketSeconds = 60): ClusterCurves {
  return {
    window: { from: iso(0), to: iso(60) },
    bucket_seconds: bucketSeconds,
    agent_ids: [1, 2],
    unpriced_calls: 0,
    buckets,
  };
}

function bucket(minutes: number, over: Partial<ClusterCurves["buckets"][number]> = {}) {
  return {
    ts: iso(minutes),
    costs: [],
    active_agents: 0,
    messages: 0,
    queue_samples: 0,
    queue_p50_seconds: null,
    queue_p95_seconds: null,
    ...over,
  };
}

describe("curves", () => {
  it("stacks cost per minute in lane order, unnamed agents on top", () => {
    const data = curves(
      [
        bucket(0, {
          costs: [
            { agent_id: 3, calls: 1, cost_usd: 0.5 },
            { agent_id: 2, calls: 1, cost_usd: 1 },
            { agent_id: 1, calls: 1, cost_usd: 0 },
          ],
        }),
      ],
      30,
    );
    const { segments, max } = costSegments(data, [2, 1]);
    expect(segments.map((s) => [s.agentId, s.y0, s.y1])).toEqual([
      [2, 0, 2],
      [3, 2, 3],
    ]);
    expect(max).toBe(3);
    expect(segments[0].x1 - segments[0].x0).toBe(30_000);
  });

  it("reads active agents, message rate and claimed queue times per bucket", () => {
    const data = curves(
      [
        bucket(0, { active_agents: 2, messages: 3, queue_samples: 2, queue_p50_seconds: 1, queue_p95_seconds: 4 }),
        bucket(1, { active_agents: 1 }),
      ],
      30,
    );
    expect(activeSeries(data).map((p) => p.value)).toEqual([2, 1]);
    expect(messageSeries(data).map((p) => p.value)).toEqual([6]);
    expect(queueSeries(data)).toMatchObject([{ p50: 1, p95: 4, samples: 2 }]);
  });

  it("rounds a scale up to 1, 2 or 5 times a power of ten", () => {
    expect([0, 0.013, 1, 1.01, 2.4, 7, 120].map(niceMax)).toEqual([1, 0.02, 1, 2, 5, 10, 200]);
  });

  it("maps values and times onto a plot", () => {
    expect(plotY(0, 10, 40)).toBe(40);
    expect(plotY(10, 10, 40)).toBe(0);
    expect(plotY(99, 10, 40)).toBe(0);
    expect(timeX(T0 + 30_000, { from: T0, to: T0 + 60_000 }, 200)).toBe(100);
  });
});

describe("formatting", () => {
  it("shortens money and durations", () => {
    expect(formatUsd(0)).toBe("$0");
    expect(formatUsd(0.00421)).toBe("$0.0042");
    expect(formatUsd(3.456)).toBe("$3.46");
    expect(formatUsd(1234.5)).toBe("$1235");
    expect(formatSeconds(0.25)).toBe("250ms");
    expect(formatSeconds(3.21)).toBe("3.2s");
    expect(formatSeconds(125)).toBe("2m05s");
    expect(formatSeconds(3 * 3600 + 120)).toBe("3h02m");
  });
});

function lane(id: number, parent: number | null, depth: number): AgentLane {
  return {
    agent_id: id,
    parent,
    kind: parent === null ? "root" : "spawn",
    depth,
    status: "idling",
    spawned_at: iso(0),
    calls: 0,
    cost_usd: 0,
    nodes: [],
    bars: [],
    events: [],
  };
}

// 1 ─ 2 ─ 3, 1 ─ 4, 5 (second root)
const LANES = [lane(1, null, 0), lane(2, 1, 1), lane(3, 2, 2), lane(4, 1, 1), lane(5, null, 0)];

describe("the lane tree", () => {
  it("marks lanes with children and folds a whole subtree", () => {
    const open = visibleRows(LANES, new Set());
    expect(open.map((r) => [r.lane.agent_id, r.hasChildren])).toEqual([
      [1, true],
      [2, true],
      [3, false],
      [4, false],
      [5, false],
    ]);
    const folded = visibleRows(LANES, new Set([1]));
    expect(folded.map((r) => r.lane.agent_id)).toEqual([1, 5]);
    expect(folded[0]).toMatchObject({ folded: true, hidden: 3 });
    const inner = visibleRows(LANES, new Set([2]));
    expect(inner.map((r) => r.lane.agent_id)).toEqual([1, 2, 4, 5]);
  });

  it("ignores a fold on a lane without children", () => {
    expect(visibleRows(LANES, new Set([3])).map((r) => r.lane.agent_id)).toEqual([1, 2, 3, 4, 5]);
  });

  it("finds the lane an agent is drawn on", () => {
    const rows = visibleRows(LANES, new Set([1]));
    const parents = parentMap(LANES);
    expect(visibleLaneOf(3, rows, parents)).toBe(1);
    expect(visibleLaneOf(5, rows, parents)).toBe(5);
    expect(visibleLaneOf(99, rows, parents)).toBeNull();
  });

  it("gives every agent its own colour", () => {
    const colors = agentColors(LANES);
    expect(new Set(colors.values()).size).toBe(LANES.length);
  });
});

function edge(sender: number, receiver: number, readAfter: number | null = 1): MessageEdge {
  return {
    inbound_id: 1,
    sender,
    receiver,
    sent_at: iso(10),
    read_at: readAfter === null ? null : iso(10 + readAfter),
    preview: "",
  };
}

describe("message edges", () => {
  it("moves ends under a fold to the folded lane and drops edges inside it", () => {
    const parents = parentMap(LANES);
    const rows = visibleRows(LANES, new Set([1]));
    const drawn = drawnEdges([edge(1, 5), edge(3, 5), edge(3, 4), edge(2, 99)], rows, parents);
    expect(drawn.map((d) => [d.from, d.to, d.aggregated])).toEqual([
      [1, 5, false],
      [1, 5, true],
    ]);
  });

  it("measures queue time from sent to read, null while unread", () => {
    expect(edgeQueueSeconds(edge(1, 2, 2))).toBe(120);
    expect(edgeQueueSeconds(edge(1, 2, null))).toBeNull();
  });

  it("draws a curve from the sender's point to the receiver's", () => {
    expect(edgePath(10, 20, 110, 60)).toBe("M 10 20 C 60 20, 60 60, 110 60");
    expect(edgePath(10, 20, 10, 60)).toBe("M 10 20 C 22 20, -2 60, 10 60");
  });
});

describe("the fetch window", () => {
  const base = { from: T0, to: T0 + 600 * 60_000 };
  const view = { from: T0 + 100 * 60_000, to: T0 + 160 * 60_000 };

  it("reaches half a view past each side, inside the base", () => {
    expect(expandedWindow(view, base)).toEqual({ from: T0 + 70 * 60_000, to: T0 + 190 * 60_000 });
    expect(expandedWindow({ from: T0, to: T0 + 10 * 60_000 }, base)).toEqual({
      from: T0,
      to: T0 + 15 * 60_000,
    });
  });

  it("keeps the loaded window while the view stays inside and is not far finer", () => {
    const loaded = expandedWindow(view, base);
    const panned = { from: view.from + 20 * 60_000, to: view.to + 20 * 60_000 };
    expect(nextFetchWindow(loaded, panned, base)).toBe(loaded);
  });

  it("refetches when the view leaves the window or zooms in far", () => {
    const loaded = expandedWindow(view, base);
    const away = { from: view.from + 50 * 60_000, to: view.to + 50 * 60_000 };
    expect(nextFetchWindow(loaded, away, base)).toEqual(expandedWindow(away, base));
    const zoomed = { from: view.from + 25 * 60_000, to: view.from + 35 * 60_000 };
    expect(nextFetchWindow(loaded, zoomed, base)).toEqual(expandedWindow(zoomed, base));
    expect(nextFetchWindow(null, view, base)).toEqual(expandedWindow(view, base));
  });

  it("names a window in whole seconds", () => {
    expect(windowIso({ from: T0 + 1500, to: T0 + 4200 })).toEqual({
      from: "2026-10-04T12:00:01.000Z",
      to: "2026-10-04T12:00:05.000Z",
    });
  });
});
