import { describe, expect, it } from "vitest";

import { categoryColor } from "@/lib/context-colors";
import type { RunTimelineMessagePart, RunTimelineNode, RunTimelineRequest, RunTimelineUnit } from "@/lib/contracts/types";

import {
  axisBox,
  axisMapTicks,
  axisTicks,
  buildAxisMap,
  chainIds,
  GAP_SHARE,
  layoutSpans,
  MIN_BLOCK_SHARE,
  panView,
  projectBox,
  zoomView,
  pendingSpans,
  cacheHitRate,
  clampViewport,
  MIN_VIEW_MS,
  panViewport,
  zoomViewport,
  firstLine,
  isSelected,
  layoutRow,
  levelsTopFirst,
  nodeWindow,
  BLOCK_CLASSES,
  classColor,
  partsForUnit,
  spanBox,
  unitColor,
  unitWindow,
  categoryClass,
  classCategory,
  contextPoint,
  hoverLit,
  inboundSources,
  matchesHighlight,
  maxInput,
  nodeAncestors,
  nodeChildren,
} from "./timeline-model";

const WINDOW = { from: "2026-10-04T12:00:00Z", to: "2026-10-04T14:00:00Z" };

function node(level: number, id = "1"): RunTimelineNode {
  return {
    id,
    level,
    parent: null,
    start: "2026-10-04T12:30:00.123456Z",
    end: "2026-10-04T13:00:00.654321Z",
    span_start: 0,
    span_end: 3,
    summary: "s",
    usage: { calls: 0, input: 0, cache_read: 0, output: 0 },
    generation: null,
  };
}

function unit(partial: Partial<RunTimelineUnit>): RunTimelineUnit {
  return {
    kind: "call",
    i0: 1,
    i1: 2,
    start: "2026-10-04T12:00:00Z",
    end: "2026-10-04T12:00:00Z",
    source: null,
    preview: "",
    parent: null,
    context_tokens: null, generation_tokens: null, estimated: null,
    ...partial,
  };
}

function part(kind: RunTimelineMessagePart["kind"]): RunTimelineMessagePart {
  return { kind, chars: 1, text: "x", text_truncated: false };
}

describe("levelsTopFirst", () => {
  it("lists each level once, the topmost first", () => {
    expect(levelsTopFirst([node(1), node(3), node(1), node(2)])).toEqual([3, 2, 1]);
    expect(levelsTopFirst([])).toEqual([]);
  });
});

describe("spanBox", () => {
  it("places a span as percentages of the window", () => {
    expect(spanBox("2026-10-04T12:30:00Z", "2026-10-04T13:00:00Z", WINDOW)).toEqual({
      left: 25,
      width: 25,
    });
  });

  it("clamps a span that overhangs the window and drops one outside it", () => {
    expect(spanBox("2026-10-04T11:00:00Z", "2026-10-04T12:30:00Z", WINDOW)).toEqual({
      left: 0,
      width: 25,
    });
    expect(spanBox("2026-10-04T15:00:00Z", "2026-10-04T16:00:00Z", WINDOW)).toBeNull();
    expect(spanBox("2026-10-04T10:00:00Z", "2026-10-04T11:00:00Z", WINDOW)).toBeNull();
  });

  it("gives an instant a zero width, left for the renderer's minimum", () => {
    expect(spanBox("2026-10-04T13:00:00Z", "2026-10-04T13:00:00Z", WINDOW)).toEqual({
      left: 50,
      width: 0,
    });
  });
});

describe("drill windows", () => {
  it("a node drills to exactly its span, microseconds kept", () => {
    expect(nodeWindow(node(1))).toEqual({
      from: "2026-10-04T12:30:00.123456Z",
      to: "2026-10-04T13:00:00.654321Z",
    });
  });

  it("a unit drills to its extent, and an instant gets one second", () => {
    expect(unitWindow(unit({ end: "2026-10-04T12:05:00Z" }))).toEqual({
      from: "2026-10-04T12:00:00Z",
      to: "2026-10-04T12:05:00Z",
    });
    expect(unitWindow(unit({}))).toEqual({
      from: "2026-10-04T12:00:00Z",
      to: "2026-10-04T12:00:01.000Z",
    });
  });
});

describe("selection", () => {
  it("matches a node by id and a unit by span and kind", () => {
    expect(isSelected({ kind: "node", id: "7" }, { kind: "node", id: "7" })).toBe(true);
    expect(isSelected({ kind: "node", id: "7" }, { kind: "node", id: "8" })).toBe(false);
    const sel = { kind: "unit", i0: 2, i1: 2, unitKind: "text" } as const;
    expect(isSelected(sel, { ...sel })).toBe(true);
    expect(isSelected(sel, { ...sel, unitKind: "thinking" })).toBe(false);
    expect(isSelected(sel, { kind: "node", id: "2" })).toBe(false);
    expect(isSelected(null, sel)).toBe(false);
  });
});

describe("units", () => {
  it("each of thinking, text, tool call and tool output reads its own kind of part; inbound and note whole", () => {
    const parts = [part("think"), part("text"), part("call"), part("out")];
    expect(partsForUnit("thinking", parts).map((p) => p.kind)).toEqual(["think"]);
    expect(partsForUnit("text", parts).map((p) => p.kind)).toEqual(["text"]);
    expect(partsForUnit("call", parts).map((p) => p.kind)).toEqual(["call"]);
    expect(partsForUnit("output", parts).map((p) => p.kind)).toEqual(["out"]);
    expect(partsForUnit("inbound", parts)).toHaveLength(4);
    expect(partsForUnit("note", parts)).toHaveLength(4);
  });

  it("every kind of block has its own color, the context breakdown's, human and agent inbound apart", () => {
    const colors = BLOCK_CLASSES.map((kind) => classColor(kind));
    expect(new Set(colors).size).toBe(BLOCK_CLASSES.length);
    expect(unitColor({ kind: "inbound", source: "agent:7" })).toBe(categoryColor("agent_messages"));
    expect(unitColor({ kind: "inbound", source: "user" })).toBe(categoryColor("user_input"));
    expect(unitColor({ kind: "thinking", source: null })).toBe(categoryColor("reasoning"));
    expect(unitColor({ kind: "call", source: null })).toBe(categoryColor("tool_call"));
    expect(unitColor({ kind: "output", source: null })).toBe(categoryColor("tool_response"));
    expect(unitColor({ kind: "text", source: null })).toBe(categoryColor("output"));
  });
});

describe("text helpers", () => {
  it("firstLine skips blank lines and clips", () => {
    expect(firstLine("\n\n  hello world\nmore", 50)).toBe("hello world");
    expect(firstLine("abcdefghij", 5)).toBe("abcd…");
  });

  it("cacheHitRate is the cache share of input, null without input", () => {
    expect(cacheHitRate({ input: 200, cache_read: 150 })).toBe(0.75);
    expect(cacheHitRate({ input: 0, cache_read: 0 })).toBeNull();
  });
});

describe("viewport", () => {
  const base = { from: 0, to: 10_000_000 };

  it("zooming keeps the instant under the cursor fixed", () => {
    const view = zoomViewport(base, base, 0.25, 0.5);
    expect(view.to - view.from).toBe(5_000_000);
    expect(view.from + 0.25 * (view.to - view.from)).toBe(2_500_000);
  });

  it("zoom stops at the minimum span and at the whole extent", () => {
    let view = base;
    for (let i = 0; i < 60; i++) view = zoomViewport(view, base, 0.5, 0.5);
    expect(view.to - view.from).toBe(MIN_VIEW_MS);
    expect(zoomViewport(base, base, 0.5, 4)).toEqual(base);
  });

  it("panning stays inside the base extent", () => {
    const view = { from: 1_000_000, to: 2_000_000 };
    expect(panViewport(view, base, 0.5)).toEqual({ from: 1_500_000, to: 2_500_000 });
    expect(panViewport(view, base, -5)).toEqual({ from: 0, to: 1_000_000 });
    expect(clampViewport({ from: -5, to: 20_000_000 }, base)).toEqual(base);
  });
});

describe("axisTicks", () => {
  it("lays out about six round ticks across the view", () => {
    const view = { from: Date.parse("2026-10-04T12:00:00Z"), to: Date.parse("2026-10-04T16:00:00Z") };
    const ticks = axisTicks(view);
    expect(ticks.length).toBeGreaterThanOrEqual(3);
    expect(ticks.length).toBeLessThanOrEqual(7);
    expect(ticks.every((tick) => tick.left >= 0 && tick.left <= 100)).toBe(true);
  });

  it("reaches millisecond labels when zoomed far in", () => {
    const from = Date.parse("2026-10-04T12:00:00Z");
    const ticks = axisTicks({ from, to: from + 300 });
    expect(ticks[0].label).toMatch(/:\d\d\.\d{3}$/);
  });
});


describe("chainIds", () => {
  const nodes = [
    { ...node(1, "a"), parent: "b" },
    { ...node(2, "b"), parent: "c" },
    { ...node(3, "c"), parent: null },
    { ...node(1, "x"), parent: "b" },
  ];
  const units = [unit({ i0: 4, i1: 5, kind: "text", parent: "a" }), unit({ i0: 9, i1: 9, kind: "note" })];

  it("is the node and every ancestor above it", () => {
    expect([...chainIds({ kind: "node", id: "a" }, nodes, [])].sort()).toEqual(["a", "b", "c"]);
    expect([...chainIds({ kind: "node", id: "c" }, nodes, [])]).toEqual(["c"]);
  });

  it("starts a message block at its covering leaf", () => {
    const chain = chainIds({ kind: "unit", i0: 4, i1: 5, unitKind: "text" }, nodes, units);
    expect([...chain].sort()).toEqual(["a", "b", "c"]);
  });

  it("is empty for a block no leaf covers, no selection, or an unloaded parent", () => {
    expect(chainIds({ kind: "unit", i0: 9, i1: 9, unitKind: "note" }, nodes, units).size).toBe(0);
    expect(chainIds(null, nodes, units).size).toBe(0);
    expect([...chainIds({ kind: "node", id: "b" }, [nodes[1]], [])]).toEqual(["b"]);
  });
});

describe("pendingSpans", () => {
  const open = (id: string, level: number, first: number, last: number, parent: string | null = null) => ({
    ...node(level, id),
    parent,
    span_start: first,
    span_end: last,
    start: `2026-10-04T12:0${first}:00Z`,
    end: `2026-10-04T12:0${last}:00Z`,
  });

  it("covers the nodes one level down that have no parent, contiguous ones merged", () => {
    const nodes = [
      open("a", 1, 0, 1, "p"),
      open("b", 1, 2, 3),
      open("c", 1, 4, 5),
      open("d", 1, 7, 8),
      open("p", 2, 0, 1),
    ];
    expect(pendingSpans(nodes, 2)).toEqual([
      { from: "2026-10-04T12:02:00Z", to: "2026-10-04T12:05:00Z" },
      { from: "2026-10-04T12:07:00Z", to: "2026-10-04T12:08:00Z" },
    ]);
    expect(pendingSpans(nodes, 1)).toEqual([]);
  });
});

describe("layoutRow", () => {
  const VIEW = { from: "2026-10-04T12:00:00.000Z", to: "2026-10-04T12:00:01.000Z" };
  const at = (ms: number) => new Date(Date.parse(VIEW.from) + ms).toISOString();
  const item = (key: string, from: number, to: number) => ({ key, start: at(from), end: at(to) });
  const bodies = (places: ReturnType<typeof layoutRow>) =>
    places.filter((place) => !place.marker).sort((a, b) => a.left - b.left);

  function expectDisjoint(places: ReturnType<typeof layoutRow>) {
    const sorted = bodies(places);
    sorted.forEach((place, i) => {
      if (i > 0) expect(place.left).toBeGreaterThanOrEqual(sorted[i - 1].left + sorted[i - 1].width - 1e-6);
    });
  }

  it("draws an empty block next to its successor as a marker, leaving the successor whole", () => {
    const places = layoutRow([item("a", 100, 100), item("b", 100, 400)], VIEW, 1000);
    expect(places.find((place) => place.key === "a")).toMatchObject({ marker: true, width: 0 });
    const b = places.find((place) => place.key === "b");
    expect(b?.marker).toBe(false);
    expect(b?.left).toBeCloseTo(100);
    expect(b?.width).toBeCloseTo(300);
    expectDisjoint(places);
  });

  it("lets a point grow to the minimum width only into the free space before the next block", () => {
    const roomy = layoutRow([item("a", 100, 100), item("b", 110, 200)], VIEW, 1000);
    expect(roomy.find((place) => place.key === "a")).toMatchObject({ marker: false, width: 3 });
    const tight = layoutRow([item("a", 100, 100), item("b", 102, 200)], VIEW, 1000);
    const a = tight.find((place) => place.key === "a");
    expect(a).toMatchObject({ marker: false, width: 2 });
    expectDisjoint(tight);
  });

  it("keeps a point followed by a duration disjoint and the point in the bar's own space", () => {
    const places = layoutRow([item("call", 500, 500), item("out", 500, 800)], VIEW, 1000);
    expect(places.map((place) => place.marker)).toEqual([true, false]);
    expectDisjoint(places);
  });

  it("stacks consecutive coincident points in separate lanes", () => {
    const places = layoutRow(
      [item("a", 200, 200), item("b", 200, 200), item("c", 200, 200), item("d", 200, 500)],
      VIEW,
      1000,
    );
    const markers = places.filter((place) => place.marker);
    expect(markers).toHaveLength(3);
    expect(new Set(markers.map((place) => place.lane)).size).toBe(3);
    expectDisjoint(places);
  });

  it("never lets a block run past the track and omits blocks outside the window", () => {
    const places = layoutRow([item("end", 999, 1000), item("gone", 5000, 6000)], VIEW, 1000);
    expect(places.map((place) => place.key)).toEqual(["end"]);
    expect(places[0].left + places[0].width).toBeLessThanOrEqual(1000);
  });
});


describe("highlight and hover model", () => {
  const unitAt = (kind: RunTimelineUnit["kind"], i0: number, source: string | null = null): RunTimelineUnit => ({
    kind,
    i0,
    i1: i0,
    start: "2026-10-04T12:00:00Z",
    end: "2026-10-04T12:00:01Z",
    source,
    preview: "p",
    parent: null,
    context_tokens: null,
    generation_tokens: null,
    estimated: null,
  });
  const treeNode = (id: string, level: number, parent: string | null, span: [number, number]): RunTimelineNode => ({
    ...node(level, id),
    parent,
    span_start: span[0],
    span_end: span[1],
  });
  const nodes = [treeNode("a", 1, "c", [0, 4]), treeNode("b", 1, "c", [5, 9]), treeNode("c", 2, null, [0, 9])];

  it("matches a block by class, and by source when one is given", () => {
    const fromTwelve = unitAt("inbound", 1, "agent:12");
    expect(matchesHighlight(fromTwelve, { cls: "agent", source: null })).toBe(true);
    expect(matchesHighlight(fromTwelve, { cls: "agent", source: "agent:12" })).toBe(true);
    expect(matchesHighlight(fromTwelve, { cls: "agent", source: "agent:9" })).toBe(false);
    expect(matchesHighlight(fromTwelve, { cls: "human", source: null })).toBe(false);
    expect(matchesHighlight(unitAt("call", 2), { cls: "call", source: null })).toBe(true);
  });

  it("lists the distinct sources of one inbound class in order of appearance", () => {
    const units = [unitAt("inbound", 1, "agent:9"), unitAt("inbound", 2, "user"), unitAt("inbound", 3, "agent:9"), unitAt("inbound", 4, "agent:3")];
    expect(inboundSources(units, "agent")).toEqual(["agent:9", "agent:3"]);
    expect(inboundSources(units, "human")).toEqual(["user"]);
  });

  it("maps breakdown categories to block classes and back", () => {
    for (const cls of BLOCK_CLASSES) expect(categoryClass(classCategory(cls))).toBe(cls);
    expect(categoryClass("system_prompt")).toBeNull();
  });

  it("hovering a block lights its chain; hovering a node lights its chain and the blocks it covers", () => {
    const units = [unitAt("text", 2), unitAt("text", 7)];
    units[0] = { ...units[0], parent: "a" };
    units[1] = { ...units[1], parent: "b" };
    const onUnit = hoverLit({ kind: "unit", i0: 2, i1: 2, unitKind: "text" }, nodes, units);
    expect([...onUnit.nodeIds].sort()).toEqual(["a", "c"]);
    expect(onUnit.unitKeys.size).toBe(0);
    const onNode = hoverLit({ kind: "node", id: "b" }, nodes, units);
    expect([...onNode.nodeIds].sort()).toEqual(["b", "c"]);
    expect([...onNode.unitKeys]).toEqual(["text-7-7"]);
    expect(hoverLit({ kind: "request", idx: 2 }, nodes, units).nodeIds.size).toBe(0);
    expect(hoverLit(null, nodes, units).unitKeys.size).toBe(0);
  });

  it("walks a node's loaded ancestors and children", () => {
    expect(nodeAncestors(nodes[0], nodes).map((n) => n.id)).toEqual(["c"]);
    expect(nodeAncestors(nodes[2], nodes)).toEqual([]);
    expect(nodeChildren(nodes[2], nodes).map((n) => n.id)).toEqual(["a", "b"]);
  });

  const request = (idx: number, iso: string, tokens = 10): RunTimelineRequest => ({ idx, ts: iso, session: 0, input_tokens: tokens, output_tokens: 0 });
  const requests = [request(2, "2026-10-04T12:10:00Z", 50), request(8, "2026-10-04T12:50:00Z", 20)];
  const view = (from: string, to: string) => ({ from: Date.parse(from), to: Date.parse(to) });

  it("follows a selection's own message, else the last request in view, else the nearest", () => {
    const whole = view("2026-10-04T12:00:00Z", "2026-10-04T13:00:00Z");
    expect(contextPoint({ kind: "unit", i0: 5, i1: 5, unitKind: "text" }, nodes, requests, whole)).toBe(5);
    expect(contextPoint({ kind: "node", id: "b" }, nodes, requests, whole)).toBe(5);
    expect(contextPoint(null, nodes, requests, whole)).toBe(8);
    expect(contextPoint(null, nodes, requests, view("2026-10-04T12:00:00Z", "2026-10-04T12:30:00Z"))).toBe(2);
    // nothing in view: the last one before it
    expect(contextPoint(null, nodes, requests, view("2026-10-04T12:20:00Z", "2026-10-04T12:30:00Z"))).toBe(2);
    expect(contextPoint(null, nodes, requests, view("2026-10-04T11:00:00Z", "2026-10-04T11:30:00Z"))).toBe(2);
    expect(contextPoint(null, nodes, [], whole)).toBeNull();
  });

  it("scales the context row to the largest input", () => {
    expect(maxInput(requests)).toBe(50);
    expect(maxInput([])).toBe(0);
  });
});

describe("hybrid axis", () => {
  const T0 = Date.parse("2026-10-04T12:00:00Z");
  const at = (sec: number) => new Date(T0 + sec * 1000).toISOString();
  const BASE = { from: T0, to: T0 + 1000 * 1000 };
  const block = (i0: number, from: number, to: number, tokens: number | null): RunTimelineUnit =>
    unit({ kind: "text", i0, i1: i0, start: at(from), end: at(to), context_tokens: tokens });
  const sample = [block(0, 10, 20, 1000), block(1, 20, 30, 3000), block(2, 500, 510, null), block(3, 510, 520, 1000)];

  it("sizes a block by its tokens and a gap by the log of its idle seconds", () => {
    const axis = buildAxisMap(sample, BASE, "hybrid");
    const [a, b, none, d] = sample.map((u) => axis.unitSpan(u));
    const width = (s: { u0: number; u1: number }) => s.u1 - s.u0;
    expect(width(b) / width(a)).toBeCloseTo(3);
    expect(width(d) / width(a)).toBeCloseTo(1);
    // No token count gets the minimum share of the block weight.
    expect(width(none)).toBeCloseTo(MIN_BLOCK_SHARE * 5000);
    // Touching blocks leave no gap; the gaps are the leading, the 470 s and the trailing idle.
    expect(b.u0).toBeCloseTo(a.u1);
    const k = (GAP_SHARE * (5000 + MIN_BLOCK_SHARE * 5000)) / (Math.log1p(10) + Math.log1p(470) + Math.log1p(480));
    expect(a.u0).toBeCloseTo(k * Math.log1p(10));
    expect(none.u0 - b.u1).toBeCloseTo(k * Math.log1p(470));
    expect(axis.total).toBeCloseTo(5000 + MIN_BLOCK_SHARE * 5000 + GAP_SHARE * (5000 + MIN_BLOCK_SHARE * 5000));
  });

  it("is a monotone, invertible map through the blocks and gaps", () => {
    const axis = buildAxisMap(sample, BASE, "hybrid");
    let last = -1;
    for (let sec = 0; sec <= 1000; sec += 7) {
      const u = axis.toU(T0 + sec * 1000);
      expect(u).toBeGreaterThanOrEqual(last);
      last = u;
      expect(axis.fromU(u)).toBeCloseTo(T0 + sec * 1000, 3);
    }
    expect(axis.toU(BASE.from)).toBe(0);
    expect(axis.toU(BASE.to)).toBeCloseTo(axis.total);
    // Half-way through a block is half-way through its width.
    const a = axis.unitSpan(sample[0]);
    expect(axis.toU(T0 + 15_000)).toBeCloseTo((a.u0 + a.u1) / 2);
  });

  it("keeps blocks of no duration visible and in order", () => {
    const point = unit({ kind: "call", i0: 1, i1: 1, start: at(100), end: at(100), context_tokens: 500 });
    const after = block(2, 100, 110, 500);
    const axis = buildAxisMap([after, point], BASE, "hybrid");
    const p = axis.unitSpan(point);
    expect(p.u1 - p.u0).toBeGreaterThan(0);
    expect(axis.unitSpan(after).u0).toBeCloseTo(p.u1);
    expect(axis.toU(T0 + 100_000, "lo")).toBeCloseTo(p.u0);
    expect(axis.toU(T0 + 100_000, "hi")).toBeCloseTo(p.u1);
  });

  it("makes a node span the blocks its message range covers, and its own times when none are loaded", () => {
    const axis = buildAxisMap(sample, BASE, "hybrid");
    const covered = axis.nodeSpan({ start: at(0), end: at(1000), span_start: 1, span_end: 3 });
    expect(covered.u0).toBeCloseTo(axis.unitSpan(sample[1]).u0);
    expect(covered.u1).toBeCloseTo(axis.unitSpan(sample[3]).u1);
    const loose = axis.nodeSpan({ start: at(20), end: at(30), span_start: 40, span_end: 41 });
    expect(loose.u0).toBeCloseTo(axis.toU(T0 + 20_000, "lo"));
    expect(loose.u1).toBeCloseTo(axis.toU(T0 + 30_000, "hi"));
  });

  it("falls back to the linear time map without blocks, and in time mode", () => {
    const empty = buildAxisMap([], BASE, "hybrid");
    expect(empty.mode).toBe("time");
    const time = buildAxisMap(sample, BASE, "time");
    expect(time.toU(T0 + 250_000)).toBe(250_000);
    expect(time.unitSpan(sample[0])).toEqual({ u0: 10_000, u1: 20_000 });
    expect(time.nodeSpan({ start: at(5), end: at(8), span_start: 0, span_end: 3 })).toEqual({ u0: 5000, u1: 8000 });
  });

  it("gives a view the same boxes in time mode as the plain window math", () => {
    const time = buildAxisMap(sample, BASE, "time");
    const view = { from: T0 + 100_000, to: T0 + 600_000 };
    const box = axisBox(time, at(200), at(300), time.viewU(view));
    expect(box).toEqual(spanBox(at(200), at(300), { from: new Date(view.from).toISOString(), to: new Date(view.to).toISOString() }));
    expect(projectBox(0, 1, { from: 2, to: 3 })).toBeNull();
  });

  it("zooms and pans in axis coordinates and stays inside the extent", () => {
    const axis = buildAxisMap(sample, BASE, "hybrid");
    const base = BASE;
    const zoomed = zoomView(axis, base, base, 0.5, 0.25);
    const vu = axis.viewU(zoomed);
    expect(vu.to - vu.from).toBeCloseTo(axis.total * 0.25, 0);
    const panned = panView(axis, zoomed, base, 1);
    expect(axis.viewU(panned).from).toBeGreaterThan(vu.from);
    expect(panned.to).toBeLessThanOrEqual(base.to);
    let view = base;
    for (let i = 0; i < 60; i++) view = zoomView(axis, view, base, 0.5, 0.5);
    expect(view.to).toBeGreaterThan(view.from);
    expect(zoomView(axis, base, base, 0.5, 4)).toEqual(base);
  });

  it("zooming in time mode matches the plain viewport zoom", () => {
    const time = buildAxisMap(sample, BASE, "time");
    expect(zoomView(time, BASE, BASE, 0.25, 0.5)).toEqual(zoomViewport(BASE, BASE, 0.25, 0.5));
    expect(panView(time, BASE, BASE, 0.1)).toEqual(panViewport(BASE, BASE, 0.1));
  });

  it("lays blocks out on axis coordinates like on time", () => {
    const places = layoutSpans([{ key: "a", u0: 10, u1: 20 }, { key: "b", u0: 20, u1: 40 }], { from: 0, to: 100 }, 1000);
    expect(places.map((p) => [p.left, p.width])).toEqual([[100, 100], [200, 200]]);
  });

  it("labels block starts with their times, keeps them apart, and falls back inside one block", () => {
    const axis = buildAxisMap(sample, BASE, "hybrid");
    const ticks = axisMapTicks(axis, BASE, 1000);
    expect(ticks.length).toBeGreaterThan(0);
    const lefts = ticks.map((tick) => tick.left);
    expect([...lefts].sort((x, y) => x - y)).toEqual(lefts);
    for (let i = 1; i < lefts.length; i++) expect((lefts[i] - lefts[i - 1]) * 10).toBeGreaterThanOrEqual(70);
    const edge = axis.boundaries.find((b) => b.ms === T0 + 500_000);
    expect(edge).toBeDefined();
    const label = new Date(T0 + 500_000);
    const hh = String(label.getHours()).padStart(2, "0");
    expect(ticks.map((tick) => tick.label).some((text) => text.startsWith(hh))).toBe(true);
    // A view inside one block has no block edge: round times are placed through the map.
    const inside = axisMapTicks(axis, { from: T0 + 21_000, to: T0 + 29_000 }, 1000);
    expect(inside.length).toBeGreaterThan(0);
    for (const tick of inside) {
      expect(tick.left).toBeGreaterThanOrEqual(0);
      expect(tick.left).toBeLessThanOrEqual(100);
    }
    expect(axisMapTicks(buildAxisMap(sample, BASE, "time"), BASE, 1000)).toEqual(axisTicks(BASE));
  });
});
