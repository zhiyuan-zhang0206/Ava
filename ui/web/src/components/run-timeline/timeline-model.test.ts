import { describe, expect, it } from "vitest";

import { categoryColor } from "@/lib/context-colors";
import type { RunTimelineMessagePart, RunTimelineNode, RunTimelineMessageBar, RunTimelineUnit } from "@/lib/contracts/types";

import {
  axisBox,
  axisTicks,
  timeAxis,
  chainIds,
  layoutSpans,
  maxContextTokens,
  maxContextTotal,
  messageLit,
  messageSelection,
  messageUnits,
  unitHasMessage,
  MIN_ITEM_PX,
  panView,
  projectBox,
  zoomView,
  clampViewport,
  MIN_VIEW_MS,
  panViewport,
  zoomViewport,
  firstLine,
  isSelected,
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
  nodeAncestors,
  nodeChildren,
} from "./timeline-model";
import {
  navigate,
  revealView,
  ADDED_ROW,
  navRowIds,
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

describe("layoutSpans", () => {
  const VIEW = { from: 0, to: 1000 };
  const span = (key: string, u0: number, u1: number) => ({ key, u0, u1 });

  it("starts an item exactly at its time and draws it as wide as it lasts", () => {
    const [a] = layoutSpans([span("a", 100, 400)], VIEW, 1000);
    expect(a.left).toBeCloseTo(100);
    expect(a.width).toBeCloseTo(300);
  });

  it("gives an instant the minimum width, to the right of its time", () => {
    const [a] = layoutSpans([span("a", 250, 250)], VIEW, 1000);
    expect(a).toEqual({ key: "a", left: 250, width: MIN_ITEM_PX });
    // The scale does not enter into it: zoomed out, the instant is still that wide.
    const [far] = layoutSpans([span("a", 250, 250)], { from: 0, to: 100_000 }, 1000);
    expect(far.width).toBe(MIN_ITEM_PX);
  });

  it("does not move or shrink its neighbours: items closer than the minimum overlap", () => {
    const places = layoutSpans([span("a", 100, 100), span("b", 101, 101), span("c", 102, 300)], VIEW, 1000);
    places.forEach((p, i) => {
      expect(p.left).toBeCloseTo([100, 101, 102][i]);
      expect(p.width).toBeCloseTo([3, 3, 198][i]);
    });
  });

  it("cuts an item at the track's end and omits one outside the view", () => {
    const places = layoutSpans([span("end", 999.5, 1000), span("gone", 5000, 6000)], VIEW, 1000);
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
    expect(hoverLit({ kind: "message", idx: 99 }, nodes, units).nodeIds.size).toBe(0);
    expect(hoverLit(null, nodes, units).unitKeys.size).toBe(0);
  });

  it("walks a node's loaded ancestors and children", () => {
    expect(nodeAncestors(nodes[0], nodes).map((n) => n.id)).toEqual(["c"]);
    expect(nodeAncestors(nodes[2], nodes)).toEqual([]);
    expect(nodeChildren(nodes[2], nodes).map((n) => n.id)).toEqual(["a", "b"]);
  });

  const bar = (idx: number, iso: string, total = 10, request = true): RunTimelineMessageBar => ({
    idx,
    start: iso,
    end: iso,
    session: 0,
    context_tokens: total / 2,
    estimated: false,
    context_total: total,
    request: request ? { calls: 1, input: total, cache_read: 0, output: 0, cache_write: 0, cost_usd: 0, cost_calls: 0 } : null,
  });
  const messages = [bar(2, "2026-10-04T12:10:00Z", 50), bar(3, "2026-10-04T12:20:00Z", 60, false), bar(8, "2026-10-04T12:50:00Z", 20)];
  const view = (from: string, to: string) => ({ from: Date.parse(from), to: Date.parse(to) });

  it("follows a selection's own message, else the last LLM request in view, else the nearest", () => {
    const whole = view("2026-10-04T12:00:00Z", "2026-10-04T13:00:00Z");
    expect(contextPoint({ kind: "message", idx: 3 }, nodes, messages, whole)).toBe(3);
    expect(contextPoint({ kind: "unit", i0: 5, i1: 5, unitKind: "text" }, nodes, messages, whole)).toBe(5);
    expect(contextPoint({ kind: "node", id: "b" }, nodes, messages, whole)).toBe(5);
    // A message that was no request (3) is never the default point.
    expect(contextPoint(null, nodes, messages, whole)).toBe(8);
    expect(contextPoint(null, nodes, messages, view("2026-10-04T12:00:00Z", "2026-10-04T12:30:00Z"))).toBe(2);
    // nothing in view: the last one before it
    expect(contextPoint(null, nodes, messages, view("2026-10-04T12:20:00Z", "2026-10-04T12:30:00Z"))).toBe(2);
    expect(contextPoint(null, nodes, messages, view("2026-10-04T11:00:00Z", "2026-10-04T11:30:00Z"))).toBe(2);
    expect(contextPoint(null, nodes, [], whole)).toBeNull();
  });

  it("scales the Context size row to the largest context total and Added context to the largest message", () => {
    expect(maxContextTotal(messages)).toBe(60);
    expect(maxContextTokens(messages)).toBe(30);
    expect(maxContextTotal([])).toBe(0);
    expect(maxContextTokens([])).toBe(0);
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

describe("message bars", () => {
  const T = Date.parse("2026-10-04T12:00:00Z");
  const iso = (sec: number) => new Date(T + sec * 1000).toISOString();
  const u = (kind: RunTimelineUnit["kind"], i0: number, from: number, to: number, parent: string | null = null, i1 = i0): RunTimelineUnit =>
    unit({ kind, i0, i1, start: iso(from), end: iso(to), parent, preview: `${kind}${i0}` });
  const msg = (idx: number, from: number, to: number, total: number, request = false): RunTimelineMessageBar => ({
    idx,
    start: iso(from),
    end: iso(to),
    session: 0,
    context_tokens: 5,
    estimated: false,
    context_total: total,
    request: request ? { calls: 1, input: total - 5, cache_read: 0, output: 5, cache_write: 0, cost_usd: 0, cost_calls: 0 } : null,
  });
  const nd = (id: string, level: number, parent: string | null, from: number, to: number, s0: number, s1: number): RunTimelineNode => ({
    ...node(level, id),
    parent,
    start: iso(from),
    end: iso(to),
    span_start: s0,
    span_end: s1,
  });
  // Level 2: P over everything; level 1: A (messages 0-1), B (2-3). Message 0 and 2 are inbound; 1 and 3 are AIMessages
  // (thinking blocks, and a request each).
  const data = {
    nodes: [nd("P", 2, null, 0, 40, 0, 3), nd("A", 1, "P", 0, 20, 0, 1), nd("B", 1, "P", 20, 40, 2, 3)],
    units: [u("inbound", 0, 0, 10, "A"), u("thinking", 1, 10, 20, "A"), u("inbound", 2, 20, 30, "B"), u("thinking", 3, 30, 40, "B")],
    messages: [msg(0, 0, 10, 5), msg(1, 10, 20, 10, true), msg(2, 20, 30, 15), msg(3, 30, 40, 20, true)],
  };
  const whole = { from: T, to: T + 40_000 };
  const unitSel = (i0: number, kind: RunTimelineUnit["kind"]) => ({ kind: "unit" as const, i0, i1: i0, unitKind: kind });
  const go = (key: "left" | "right" | "up" | "down", row: string | null, selection: Parameters<typeof navigate>[1] extends infer C ? (C extends { selection: infer S } ? S : never) : never) =>
    navigate(key, { row, selection }, data, timeAxis(whole), whole);

  it("says which message a block shows: a turn block its AIMessage, an output block the messages after it", () => {
    expect(unitHasMessage({ kind: "thinking", i0: 4, i1: 4 }, 4)).toBe(true);
    expect(unitHasMessage({ kind: "call", i0: 4, i1: 4 }, 5)).toBe(false);
    expect(unitHasMessage({ kind: "inbound", i0: 7, i1: 7 }, 7)).toBe(true);
    // An output block opened by the AIMessage 4 shows the tool result 5, not the AIMessage itself.
    expect(unitHasMessage({ kind: "output", i0: 4, i1: 5 }, 5)).toBe(true);
    expect(unitHasMessage({ kind: "output", i0: 4, i1: 5 }, 4)).toBe(false);
    // A tool result with no open call is an output block of its own message.
    expect(unitHasMessage({ kind: "output", i0: 6, i1: 6 }, 6)).toBe(true);
    expect(messageUnits(1, data.units).map((x) => x.i0)).toEqual([1]);
  });

  it("selects the block that shows a message for the details pane, the AIMessage's thinking first", () => {
    const turn = [u("call", 1, 20, 20), u("text", 1, 20, 22), u("thinking", 1, 10, 20)];
    expect(messageSelection(1, turn)).toEqual(unitSel(1, "thinking"));
    expect(messageSelection(1, turn.slice(0, 2))).toEqual(unitSel(1, "text"));
    expect(messageSelection(0, data.units)).toEqual(unitSel(0, "inbound"));
    expect(messageSelection(99, data.units)).toBeNull();
  });

  it("lights a message's bars for the message itself, or any block that shows it, hovered", () => {
    expect(messageLit(1, { kind: "message", idx: 1 })).toBe(true);
    expect(messageLit(1, unitSel(1, "thinking"))).toBe(true);
    expect(messageLit(1, unitSel(0, "inbound"))).toBe(false);
    expect(messageLit(1, { kind: "node", id: "A" })).toBe(false);
    expect(messageLit(1, null)).toBe(false);
  });

  it("selecting a message lights the nodes over the blocks that show it, up to the top", () => {
    const sel = { kind: "message" as const, idx: 3 };
    expect([...chainIds(sel, data.nodes, data.units)].sort()).toEqual(["B", "P"]);
    expect(chainIds({ kind: "message", idx: 99 }, data.nodes, data.units).size).toBe(0);
    const lit = hoverLit(sel, data.nodes, data.units);
    expect([...lit.nodeIds].sort()).toEqual(["B", "P"]);
    expect([...lit.unitKeys]).toEqual([unitKey(data.units[3])]);
  });

  it("moves left and right within a row and stops at the ends", () => {
    expect(go("right", UNITS_ROW, unitSel(0, "inbound"))?.item.selection).toEqual(unitSel(1, "thinking"));
    expect(go("left", UNITS_ROW, unitSel(0, "inbound"))).toBeNull();
    expect(go("right", "level-1", { kind: "node", id: "A" })?.item.selection).toEqual({ kind: "node", id: "B" });
    expect(go("right", "level-1", { kind: "node", id: "B" })).toBeNull();
    expect(go("right", INPUT_ROW, { kind: "message", idx: 1 })?.item.selection).toEqual({ kind: "message", idx: 2 });
  });

  it("goes up to the parent and down to the first child", () => {
    expect(go("up", UNITS_ROW, unitSel(2, "inbound"))).toMatchObject({ row: "level-1", item: { selection: { id: "B" } } });
    expect(go("up", "level-1", { kind: "node", id: "B" })).toMatchObject({ row: "level-2", item: { selection: { id: "P" } } });
    expect(go("up", "level-2", { kind: "node", id: "P" })).toBeNull();
    expect(go("down", "level-2", { kind: "node", id: "P" })).toMatchObject({ row: "level-1", item: { selection: { id: "A" } } });
    expect(go("down", "level-1", { kind: "node", id: "B" })).toMatchObject({ row: UNITS_ROW, item: { selection: unitSel(2, "inbound") } });
  });

  it("moves between the Messages row and the context rows by the message a block shows", () => {
    // Messages down to the context rows: the message the block shows.
    expect(go("down", UNITS_ROW, unitSel(1, "thinking"))).toMatchObject({ row: INPUT_ROW, item: { message: { idx: 1 } } });
    expect(go("down", UNITS_ROW, unitSel(2, "inbound"))?.item.message?.idx).toBe(2);
    // Context rows up: the block that shows the message; between the two context rows: the same message.
    expect(go("up", INPUT_ROW, { kind: "message", idx: 3 })).toMatchObject({ row: UNITS_ROW, item: { selection: unitSel(3, "thinking") } });
    expect(go("down", INPUT_ROW, { kind: "message", idx: 3 })).toMatchObject({ row: ADDED_ROW, item: { message: { idx: 3 } } });
    expect(go("up", ADDED_ROW, { kind: "message", idx: 1 })).toMatchObject({ row: INPUT_ROW, item: { message: { idx: 1 } } });
    expect(go("down", ADDED_ROW, { kind: "message", idx: 1 })).toBeNull();
    // With the Context size row hidden, the Messages row goes straight down to Added context.
    const rows = navRowIds(data, { levels: null, context: "added" });
    expect(navigate("down", { row: UNITS_ROW, selection: unitSel(1, "thinking") }, data, timeAxis(whole), whole, rows)).toMatchObject({
      row: ADDED_ROW,
      item: { message: { idx: 1 } },
    });
    // A unit without a parent goes to the level-1 node covering its time.
    const orphan = { ...data, units: data.units.map((x) => (x.i0 === 2 ? { ...x, parent: null } : x)) };
    expect(navigate("up", { row: UNITS_ROW, selection: unitSel(2, "inbound") }, orphan, timeAxis(whole), whole)?.item.selection).toEqual({ kind: "node", id: "B" });
  });

  it("a node with a dangling parent goes up by overlap instead", () => {
    const dangling = { ...data, nodes: data.nodes.map((x) => (x.id === "B" ? { ...x, parent: "gone" } : x)) };
    expect(navigate("up", { row: "level-1", selection: { kind: "node", id: "B" } }, dangling, timeAxis(whole), whole)?.item.selection).toEqual({ kind: "node", id: "P" });
  });

  it("reads the row from the selection when the remembered row does not hold it, and starts at the leftmost item in view", () => {
    expect(go("right", "level-2", unitSel(0, "inbound"))?.item.selection).toEqual(unitSel(1, "thinking"));
    const inView = { from: T + 25_000, to: T + 40_000 };
    expect(navigate("right", null, data, timeAxis(whole), inView)?.item.selection).toEqual(unitSel(2, "inbound"));
    expect(navigate("left", null, { nodes: [], units: [], messages: [] }, timeAxis(whole), whole)).toBeNull();
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
  const mb = (idx: number): RunTimelineMessageBar => ({
    idx, start: iso(idx * 10), end: iso(idx * 10 + 10), session: 0, context_tokens: 1, estimated: false, context_total: 1, request: null,
  });
  // P (level 2) over A (0-1) and B (2-3); one text block and one message per index.
  const data = {
    nodes: [nd("P", 2, null, 0, 3), nd("A", 1, "P", 0, 1), nd("B", 1, "P", 2, 3)],
    units: [u(0, "A"), u(1, "A"), u(2, "B"), u(3, "B")],
    messages: [mb(0), mb(1), mb(2), mb(3)],
  };
  const keys = (roles: ReturnType<typeof selectionRoles>, row: string) => [...(roles.linked.get(row) ?? [])].sort();

  it("a message is the one primary item; it links one hop: its block, the block's ancestors, the same message in the other row", () => {
    const roles = selectionRoles({ row: INPUT_ROW, selection: { kind: "message", idx: 2 } }, data);
    expect(roles.primary).toEqual({ row: INPUT_ROW, key: "m2" });
    expect(keys(roles, UNITS_ROW)).toEqual(["utext-2-2"]);
    expect(keys(roles, "level-1")).toEqual(["nB"]);
    expect(keys(roles, "level-2")).toEqual(["nP"]);
    expect(keys(roles, ADDED_ROW)).toEqual(["m2"]);
    // Nothing links back down from the ancestors: the other messages under the same nodes stay unlit.
    expect(keys(roles, INPUT_ROW)).toEqual([]);
    expect(roles.linked.get(UNITS_ROW)?.has("utext-3-3")).toBe(false);
  });

  it("the cursor row decides which context row is primary, and the other one is linked", () => {
    const added = selectionRoles({ row: ADDED_ROW, selection: { kind: "message", idx: 2 } }, data);
    expect(added.primary).toEqual({ row: ADDED_ROW, key: "m2" });
    expect(keys(added, INPUT_ROW)).toEqual(["m2"]);
    expect(keys(added, ADDED_ROW)).toEqual([]);
  });

  it("a block links to its ancestors and the messages it shows; a node links only upward", () => {
    const block = selectionRoles({ row: UNITS_ROW, selection: { kind: "unit", i0: 2, i1: 2, unitKind: "text" } }, data);
    expect(keys(block, "level-1")).toEqual(["nB"]);
    expect(keys(block, INPUT_ROW)).toEqual(["m2"]);
    expect(keys(block, ADDED_ROW)).toEqual(["m2"]);
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
