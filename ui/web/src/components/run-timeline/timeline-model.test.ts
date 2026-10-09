import { describe, expect, it } from "vitest";

import { categoryColor } from "@/lib/context-colors";
import type { RunTimelineMessagePart, RunTimelineNode, RunTimelineRequest, RunTimelineUnit } from "@/lib/contracts/types";

import {
  axisBox,
  barBox,
  BAR_GAP_PX,
  BAR_MIN_PX,
  requestLit,
  requestReading,
  requestSpan,
  requestUnits,
  requestSelection,
  axisTicks,
  timeAxis,
  chainIds,
  layoutSpans,
  panView,
  projectBox,
  zoomView,
  clampViewport,
  MIN_VIEW_MS,
  panViewport,
  zoomViewport,
  firstLine,
  isSelected,
  layoutRow,
  levelsTopFirst,
  BLOCK_CLASSES,
  classColor,
  partsForUnit,
  spanBox,
  unitColor,
  categoryClass,
  classCategory,
  contextPoint,
  hoverLit,
  unitKey,
  inboundSources,
  matchesHighlight,
  maxAdded,
  maxInput,
  nodeAncestors,
  nodeChildren,
} from "./timeline-model";
import {
  navigate,
  revealView,
  ADDED_ROW,
  selectionRoles,
  INPUT_ROW,
  UNITS_ROW,
} from "./timeline-nav";

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
    usage: { calls: 0, input: 0, cache_read: 0, output: 0, cache_write: 0, cost_usd: 0, cost_calls: 0 },
    generation: null,
    context_tokens: null,
    estimated: null,
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

  const request = (idx: number, iso: string, tokens = 10): RunTimelineRequest => ({ idx, ts: iso, session: 0, input_tokens: tokens, output_tokens: 0, added_tokens: tokens / 2, added_estimated: false, added_from: idx - 1, added_to: idx });
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

  it("scales the added-context row to the largest addition, independently of the input", () => {
    expect(maxAdded(requests)).toBe(25);
    expect(maxAdded([])).toBe(0);
  });
});

describe("time axis", () => {
  const T0 = Date.parse("2026-10-04T12:00:00Z");
  const at = (sec: number) => new Date(T0 + sec * 1000).toISOString();
  const BASE = { from: T0, to: T0 + 1000 * 1000 };
  const axis = timeAxis(BASE);

  it("is linear in milliseconds since the extent's start, clamped to it", () => {
    expect(axis.toU(T0 + 250_000)).toBe(250_000);
    expect(axis.toU(T0 - 5)).toBe(0);
    expect(axis.toU(T0 + 5_000_000)).toBe(axis.total);
    expect(axis.fromU(250_000)).toBe(T0 + 250_000);
    expect(axis.unitSpan({ start: at(10), end: at(20) })).toEqual({ u0: 10_000, u1: 20_000 });
    expect(axis.nodeSpan({ start: at(5), end: at(8) })).toEqual({ u0: 5000, u1: 8000 });
  });

  it("gives a view the same boxes as the plain window math", () => {
    const view = { from: T0 + 100_000, to: T0 + 600_000 };
    const box = axisBox(axis, at(200), at(300), axis.viewU(view));
    expect(box).toEqual(spanBox(at(200), at(300), { from: new Date(view.from).toISOString(), to: new Date(view.to).toISOString() }));
    expect(projectBox(0, 1, { from: 2, to: 3 })).toBeNull();
  });

  it("zooms and pans like the plain viewport and stays inside the extent", () => {
    expect(zoomView(axis, BASE, BASE, 0.25, 0.5)).toEqual(zoomViewport(BASE, BASE, 0.25, 0.5));
    expect(panView(axis, BASE, BASE, 0.1)).toEqual(panViewport(BASE, BASE, 0.1));
    let view = BASE;
    for (let i = 0; i < 60; i++) view = zoomView(axis, view, BASE, 0.5, 0.5);
    expect(view.to).toBeGreaterThan(view.from);
    expect(zoomView(axis, BASE, BASE, 0.5, 4)).toEqual(BASE);
  });

  it("lays blocks out on axis coordinates like on time", () => {
    const places = layoutSpans([{ key: "a", u0: 10, u1: 20 }, { key: "b", u0: 20, u1: 40 }], { from: 0, to: 100 }, 1000);
    expect(places.map((p) => [p.left, p.width])).toEqual([[100, 100], [200, 200]]);
  });

  it("keeps round-number ticks of the viewport", () => {
    expect(axisTicks(BASE).length).toBeGreaterThan(0);
  });
});

describe("request bars", () => {
  it("spans the blocks a request read, less a gap each side, never thinner than the minimum", () => {
    const view = { from: 0, to: 1000 };
    expect(barBox({ u0: 100, u1: 300 }, view, 1000)).toEqual({ left: 100 + BAR_GAP_PX, width: 200 - 2 * BAR_GAP_PX });
    expect(barBox({ u0: 100, u1: 100.5 }, view, 1000).width).toBe(BAR_MIN_PX);
    // Zoomed to half the axis, the same span is twice as wide.
    expect(barBox({ u0: 100, u1: 300 }, { from: 0, to: 500 }, 1000).width).toBe(400 - 2 * BAR_GAP_PX);
  });

  const T = Date.parse("2026-10-04T12:00:00Z");
  const iso = (sec: number) => new Date(T + sec * 1000).toISOString();
  const u = (kind: RunTimelineUnit["kind"], i0: number, from: number, to: number, parent: string | null = null): RunTimelineUnit =>
    unit({ kind, i0, i1: i0, start: iso(from), end: iso(to), parent, preview: `${kind}${i0}` });
  const req = (idx: number, sec: number, from: number): RunTimelineRequest => ({
    idx,
    ts: iso(sec),
    session: 0,
    input_tokens: 10,
    output_tokens: 1,
    added_tokens: 1,
    added_estimated: false,
    added_from: from,
    added_to: idx,
  });
  const nd = (id: string, level: number, parent: string | null, from: number, to: number, s0: number, s1: number): RunTimelineNode => ({
    ...node(level, id),
    parent,
    start: iso(from),
    end: iso(to),
    span_start: s0,
    span_end: s1,
  });
  // Level 2: P over everything; level 1: A (messages 0-1), B (2-3); blocks 0..3; AIMessage 1 and 3 made requests.
  const data = {
    nodes: [nd("P", 2, null, 0, 40, 0, 3), nd("A", 1, "P", 0, 20, 0, 1), nd("B", 1, "P", 20, 40, 2, 3)],
    units: [u("inbound", 0, 0, 10, "A"), u("thinking", 1, 10, 20, "A"), u("inbound", 2, 20, 30, "B"), u("thinking", 3, 30, 40, "B")],
    requests: [req(1, 10, 0), req(3, 30, 1)],
  };
  const whole = { from: T, to: T + 40_000 };
  const unitSel = (i0: number, kind: RunTimelineUnit["kind"]) => ({ kind: "unit" as const, i0, i1: i0, unitKind: kind });
  const go = (key: "left" | "right" | "up" | "down", row: string | null, selection: Parameters<typeof navigate>[1] extends infer C ? (C extends { selection: infer S } ? S : never) : never) =>
    navigate(key, { row, selection }, data, timeAxis(whole), whole);

  it("maps a request to the blocks it read for the first time, and back", () => {
    const [first, second] = data.requests;
    expect(requestUnits(first, data.units).map((x) => x.i0)).toEqual([0]);
    // The second request re-reads nothing: it starts at the previous reply (message 1) and ends before its own (3).
    expect(requestUnits(second, data.units).map((x) => x.i0)).toEqual([1, 2]);
    expect(requestReading(data.units[2], data.requests)).toBe(second);
    expect(requestReading(data.units[3], data.requests)).toBeUndefined();
  });

  it("puts a request's bar from the start of its first block to the end of its last", () => {
    const axis = timeAxis(whole);
    const span = requestSpan(data.requests[1], data.units, axis);
    expect(span.u0).toBe(axis.unitSpan(data.units[1]).u0);
    expect(span.u1).toBe(axis.unitSpan(data.units[2]).u1);
  });

  it("selects the block of the AIMessage that made the request, for the details pane", () => {
    expect(requestSelection({ idx: 1 }, data.units)).toEqual(unitSel(1, "thinking"));
    expect(requestSelection({ idx: 99 }, data.units)).toBeNull();
  });

  it("lights a request's bar for the request, or any block it read, selected or hovered", () => {
    const [first, second] = data.requests;
    expect(requestLit(second, { kind: "request", idx: 3 }, null).selected).toBe(true);
    expect(requestLit(second, unitSel(2, "inbound"), null).selected).toBe(true);
    expect(requestLit(second, unitSel(3, "thinking"), null).selected).toBe(false);
    expect(requestLit(first, unitSel(2, "inbound"), null).selected).toBe(false);
    expect(requestLit(second, null, unitSel(1, "thinking")).hovered).toBe(true);
    expect(requestLit(second, null, { kind: "request", idx: 3 }).hovered).toBe(true);
  });

  it("selecting a request lights the nodes over the blocks it read, up to the top, as a selected block does", () => {
    const sel = { kind: "request" as const, idx: 3 };
    // Request 3 read blocks 1 and 2: A covers 1, B covers 2, P both.
    expect([...chainIds(sel, data.nodes, data.units, data.requests)].sort()).toEqual(["A", "B", "P"]);
    expect(chainIds({ kind: "request", idx: 99 }, data.nodes, data.units, data.requests).size).toBe(0);
    expect([...hoverLit(sel, data.nodes, data.units, data.requests).nodeIds].sort()).toEqual(["A", "B", "P"]);
  });

  it("lights every block a hovered request read, and follows a request to the context breakdown", () => {
    const lit = hoverLit({ kind: "request", idx: 3 }, data.nodes, data.units, data.requests);
    expect([...lit.unitKeys].sort()).toEqual([unitKey(data.units[1]), unitKey(data.units[2])].sort());
    expect(contextPoint({ kind: "request", idx: 3 }, data.nodes, data.requests, whole)).toBe(3);
  });

  it("moves left and right within a row and stops at the ends", () => {
    expect(go("right", UNITS_ROW, unitSel(0, "inbound"))?.item.selection).toEqual(unitSel(1, "thinking"));
    expect(go("left", UNITS_ROW, unitSel(0, "inbound"))).toBeNull();
    expect(go("right", "level-1", { kind: "node", id: "A" })?.item.selection).toEqual({ kind: "node", id: "B" });
    expect(go("right", "level-1", { kind: "node", id: "B" })).toBeNull();
    expect(go("right", INPUT_ROW, { kind: "request", idx: 1 })?.item.selection).toEqual({ kind: "request", idx: 3 });
  });

  it("goes up to the parent and down to the first child", () => {
    expect(go("up", UNITS_ROW, unitSel(2, "inbound"))).toMatchObject({ row: "level-1", item: { selection: { id: "B" } } });
    expect(go("up", "level-1", { kind: "node", id: "B" })).toMatchObject({ row: "level-2", item: { selection: { id: "P" } } });
    expect(go("up", "level-2", { kind: "node", id: "P" })).toBeNull();
    expect(go("down", "level-2", { kind: "node", id: "P" })).toMatchObject({ row: "level-1", item: { selection: { id: "A" } } });
    expect(go("down", "level-1", { kind: "node", id: "B" })).toMatchObject({ row: UNITS_ROW, item: { selection: unitSel(2, "inbound") } });
  });

  it("falls back to the item covering, else nearest to, the time when there is no parent or child", () => {
    // Messages down to the request rows: the request that read the block.
    expect(go("down", UNITS_ROW, unitSel(1, "thinking"))).toMatchObject({ row: INPUT_ROW, item: { request: { idx: 3 } } });
    expect(go("down", UNITS_ROW, unitSel(2, "inbound"))?.item.request?.idx).toBe(3);
    // Request rows up: the first block the request read; between the two request rows: the same request.
    expect(go("up", INPUT_ROW, { kind: "request", idx: 3 })).toMatchObject({ row: UNITS_ROW, item: { selection: unitSel(1, "thinking") } });
    expect(go("down", INPUT_ROW, { kind: "request", idx: 3 })).toMatchObject({ row: ADDED_ROW, item: { request: { idx: 3 } } });
    expect(go("up", ADDED_ROW, { kind: "request", idx: 1 })).toMatchObject({ row: INPUT_ROW, item: { request: { idx: 1 } } });
    expect(go("down", ADDED_ROW, { kind: "request", idx: 1 })).toBeNull();
    // A unit without a parent goes to the level-1 node covering its time.
    const orphan = { ...data, units: data.units.map((x) => (x.i0 === 2 ? { ...x, parent: null } : x)) };
    expect(navigate("up", { row: UNITS_ROW, selection: unitSel(2, "inbound") }, orphan, timeAxis(whole), whole)?.item.selection).toEqual({ kind: "node", id: "B" });
  });

  it("treats every row alike: a bar goes up and down by the same rule as a node or a block", () => {
    // Up from Context size: the blocks the request read (related), the first of them.
    expect(go("up", INPUT_ROW, { kind: "request", idx: 1 })).toMatchObject({ row: UNITS_ROW, item: { selection: unitSel(0, "inbound") } });
    // Down from Messages: the request that read the block, two rows on through the same-request link.
    const down = go("down", UNITS_ROW, unitSel(0, "inbound"));
    expect(down).toMatchObject({ row: INPUT_ROW, item: { request: { idx: 1 } } });
    expect(go("down", INPUT_ROW, { kind: "request", idx: 1 })).toMatchObject({ row: ADDED_ROW, item: { request: { idx: 1 } } });
    // With no relation (a block no request read), the context row item overlapping most on the x axis.
    expect(go("down", UNITS_ROW, unitSel(3, "thinking"))).toMatchObject({ row: INPUT_ROW, item: { request: { idx: 3 } } });
    // Up from a node with no parent in the row above overlaps instead: a level-1 node with a dangling parent.
    const dangling = { ...data, nodes: data.nodes.map((x) => (x.id === "B" ? { ...x, parent: "gone" } : x)) };
    const axis = timeAxis(whole);
    expect(navigate("up", { row: "level-1", selection: { kind: "node", id: "B" } }, dangling, axis, whole)?.item.selection).toEqual({ kind: "node", id: "P" });
  });

  it("reads the row from the selection when the remembered row does not hold it, and starts at the leftmost item in view", () => {
    expect(go("right", "level-2", unitSel(0, "inbound"))?.item.selection).toEqual(unitSel(1, "thinking"));
    const inView = { from: T + 25_000, to: T + 40_000 };
    expect(navigate("right", null, data, timeAxis(whole), inView)?.item.selection).toEqual(unitSel(2, "inbound"));
    expect(navigate("left", null, { nodes: [], units: [], requests: [] }, timeAxis(whole), whole)).toBeNull();
  });

  it("pans to an item outside the view without changing the zoom, and leaves a visible one alone", () => {
    const axis = timeAxis({ from: T, to: T + 400_000 });
    const base = { from: T, to: T + 400_000 };
    const view = { from: T, to: T + 40_000 };
    expect(revealView(axis, view, base, T + 10_000, T + 20_000)).toBe(view);
    const moved = revealView(axis, view, base, T + 200_000, T + 210_000);
    expect(moved.to - moved.from).toBe(40_000);
    expect(moved.from).toBeLessThanOrEqual(T + 200_000);
    expect(moved.to).toBeGreaterThanOrEqual(T + 210_000);
    const edge = revealView(axis, view, base, T + 399_000, T + 400_000);
    expect(edge.to).toBe(base.to);
  });
});

describe("selectionRoles", () => {
  const T = Date.parse("2026-10-04T12:00:00Z");
  const iso = (sec: number) => new Date(T + sec * 1000).toISOString();
  const u = (i0: number, parent: string | null): RunTimelineUnit =>
    unit({ kind: "text", i0, i1: i0, start: iso(i0 * 10), end: iso(i0 * 10 + 10), parent });
  const nd = (id: string, level: number, parent: string | null, s0: number, s1: number): RunTimelineNode => ({
    ...node(level, id),
    parent,
    start: iso(s0 * 10),
    end: iso(s1 * 10 + 10),
    span_start: s0,
    span_end: s1,
  });
  const rq = (idx: number, from: number): RunTimelineRequest => ({
    idx, ts: iso(idx * 10), session: 0, input_tokens: 1, output_tokens: 1, added_tokens: 1, added_estimated: false, added_from: from, added_to: idx,
  });
  // P (level 2) over A (0-1) and B (2-3); request 2 read blocks 0-1 (under A), request 4 read blocks 2-3 (under B).
  const data = {
    nodes: [nd("P", 2, null, 0, 3), nd("A", 1, "P", 0, 1), nd("B", 1, "P", 2, 3)],
    units: [u(0, "A"), u(1, "A"), u(2, "B"), u(3, "B")],
    requests: [rq(2, 0), rq(4, 2)],
  };
  const keys = (roles: ReturnType<typeof selectionRoles>, row: string) => [...(roles.linked.get(row) ?? [])].sort();

  it("a request is the one primary item; it links one hop: its blocks, their ancestors, the same request in the other row", () => {
    const roles = selectionRoles({ row: INPUT_ROW, selection: { kind: "request", idx: 2 } }, data);
    expect(roles.primary).toEqual({ row: INPUT_ROW, key: "r2" });
    expect(keys(roles, UNITS_ROW)).toEqual(["utext-0-0", "utext-1-1"]);
    expect(keys(roles, "level-1")).toEqual(["nA"]);
    expect(keys(roles, "level-2")).toEqual(["nP"]);
    expect(keys(roles, ADDED_ROW)).toEqual(["r2"]);
    // Nothing links back down from the ancestors: the other request under the same top node stays unlit.
    expect(keys(roles, INPUT_ROW)).toEqual([]);
    expect(roles.linked.get(UNITS_ROW)?.has("utext-2-2")).toBe(false);
  });

  it("the cursor row decides which context row is primary, and the other one is linked", () => {
    const added = selectionRoles({ row: ADDED_ROW, selection: { kind: "request", idx: 2 } }, data);
    expect(added.primary).toEqual({ row: ADDED_ROW, key: "r2" });
    expect(keys(added, INPUT_ROW)).toEqual(["r2"]);
    expect(keys(added, ADDED_ROW)).toEqual([]);
  });

  it("a block links to its ancestors and the request that read it; a node links only upward", () => {
    const block = selectionRoles({ row: UNITS_ROW, selection: { kind: "unit", i0: 2, i1: 2, unitKind: "text" } }, data);
    expect(keys(block, "level-1")).toEqual(["nB"]);
    expect(keys(block, INPUT_ROW)).toEqual(["r4"]);
    expect(keys(block, ADDED_ROW)).toEqual(["r4"]);
    const top = selectionRoles({ row: "level-2", selection: { kind: "node", id: "P" } }, data);
    expect(top.primary).toEqual({ row: "level-2", key: "nP" });
    expect([...top.linked.keys()]).toEqual([]);
    const mid = selectionRoles({ row: "level-1", selection: { kind: "node", id: "A" } }, data);
    expect(keys(mid, "level-2")).toEqual(["nP"]);
    expect(mid.linked.has(INPUT_ROW)).toBe(false);
  });

  it("has no roles without a selection", () => {
    expect(selectionRoles(null, data)).toEqual({ primary: null, linked: new Map() });
  });
});
