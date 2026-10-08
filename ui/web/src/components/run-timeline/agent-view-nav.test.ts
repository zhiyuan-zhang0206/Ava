import { describe, expect, it } from "vitest";

import type { RunTimelineNode, RunTimelineRequest, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import { navigateAcross, placerFor, type ViewAgent } from "./agent-view-nav";
import { layoutsFor } from "./timeline-canvas-model";
import { buildSharedAxisMap } from "./timeline-model";
import { ADDED_ROW, INPUT_ROW, UNITS_ROW, levelRowId, navRowIds } from "./timeline-nav";

const T0 = Date.parse("2026-10-04T12:00:00.000Z");
const at = (ms: number) => new Date(T0 + ms).toISOString();
const BASE = { from: T0, to: T0 + 1000 };
const usage = { calls: 0, input: 0, cache_read: 0, output: 0, cache_write: 0, cost_usd: 0, cost_calls: 0 };

const node = (id: string, level: number, from: number, to: number): RunTimelineNode => ({
  id,
  level,
  parent: null,
  start: at(from),
  end: at(to),
  span_start: 0,
  span_end: 3,
  summary: id,
  usage,
  generation: null,
  context_tokens: null,
  estimated: null,
});

const unit = (i0: number, from: number, to: number, tokens: number | null = null): RunTimelineUnit => ({
  kind: "text",
  i0,
  i1: i0,
  start: at(from),
  end: at(to),
  source: null,
  preview: "",
  parent: null,
  context_tokens: tokens,
  generation_tokens: null,
  estimated: null,
});

const request: RunTimelineRequest = {
  idx: 1,
  ts: at(10),
  session: 0,
  input_tokens: 10,
  output_tokens: 1,
  added_tokens: 5,
  added_estimated: false,
  added_from: 0,
  added_to: 1,
};

const data = (over: Partial<RunTimelineResponse>) =>
  ({ nodes: [], units: [], events: [], requests: [], ...over }) as RunTimelineResponse;

describe("row options", () => {
  const d = data({
    nodes: [node("a", 1, 0, 100), node("b", 2, 0, 100), node("c", 3, 0, 100)],
    units: [unit(0, 0, 100)],
    requests: [request],
  });

  it("keeps the topmost levels", () => {
    expect(navRowIds(d, { levels: 2, context: "both" })).toEqual([
      levelRowId(3),
      levelRowId(2),
      UNITS_ROW,
      INPUT_ROW,
      ADDED_ROW,
    ]);
    expect(navRowIds(d, { levels: 0, context: "both" })[0]).toBe(UNITS_ROW);
  });

  it("draws the context bars asked for, and none for an agent without requests", () => {
    expect(navRowIds(d, { levels: null, context: "absolute" }).slice(-2)).toEqual([UNITS_ROW, INPUT_ROW]);
    expect(navRowIds(d, { levels: null, context: "added" }).slice(-2)).toEqual([UNITS_ROW, ADDED_ROW]);
    expect(navRowIds(d, { levels: null, context: "off" }).at(-1)).toBe(UNITS_ROW);
    expect(navRowIds(data({ units: d.units }), { levels: null, context: "both" })).toEqual([UNITS_ROW]);
  });
});

describe("shared axis", () => {
  it("keeps blocks of different agents apart although they share indices and kinds", () => {
    const a = [unit(0, 0, 100, 100)];
    const b = [unit(0, 500, 600, 300)];
    const axis = buildSharedAxisMap(
      [
        { owner: 1, units: a },
        { owner: 2, units: b },
      ],
      BASE,
      "hybrid",
    );
    const one = axis.unitSpan(a[0], 1);
    const two = axis.unitSpan(b[0], 2);
    expect(one.u1 - one.u0).toBeLessThan(two.u1 - two.u0);
    expect(two.u0).toBeGreaterThanOrEqual(one.u1);
    // A node follows only the blocks of its own agent.
    const span = axis.nodeSpan(node("n", 1, 0, 100), 1);
    expect(span).toEqual(one);
  });

  it("hands one placer per axis and agent, so cached layouts hold", () => {
    const axis = buildSharedAxisMap([{ owner: 1, units: [] }], BASE, "time");
    expect(placerFor(axis, 1)).toBe(placerFor(axis, 1));
    expect(placerFor(axis, 1)).not.toBe(placerFor(axis, 2));
  });

  it("remembers each agent's layout, not just the last one asked for", () => {
    const d1 = data({ units: [unit(0, 0, 100)] });
    const d2 = data({ units: [unit(0, 200, 300)] });
    const axis = buildSharedAxisMap(
      [
        { owner: 1, units: d1.units },
        { owner: 2, units: d2.units },
      ],
      BASE,
      "time",
    );
    const view = axis.viewU(BASE);
    const first = layoutsFor(d1, placerFor(axis, 1), view, 1000);
    layoutsFor(d2, placerFor(axis, 2), view, 1000);
    expect(layoutsFor(d1, placerFor(axis, 1), view, 1000)).toBe(first);
  });
});

describe("navigateAcross", () => {
  const agent = (id: number, d: RunTimelineResponse, axis: ReturnType<typeof buildSharedAxisMap>, context: "both" | "off" = "off"): ViewAgent => ({
    id,
    data: d,
    rows: navRowIds(d, { levels: null, context }),
    place: placerFor(axis, id),
  });
  const d1 = data({ nodes: [node("n1", 1, 0, 400)], units: [unit(0, 0, 100), unit(1, 100, 200)] });
  const d2 = data({ nodes: [node("n2", 1, 0, 400)], units: [unit(0, 300, 400)] });
  const d3 = data({});
  const axis = buildSharedAxisMap(
    [
      { owner: 1, units: d1.units },
      { owner: 2, units: d2.units },
      { owner: 3, units: d3.units },
    ],
    BASE,
    "time",
  );
  const agents = [agent(1, d1, axis), agent(2, d2, axis), agent(3, d3, axis)];
  const cursor = (id: number, row: string, key: number) => ({
    agent: id,
    row,
    selection: { kind: "unit" as const, i0: key, i1: key, unitKind: "text" as const },
  });

  it("starts at the first agent that has something in view", () => {
    const step = navigateAcross("right", null, agents, BASE);
    expect(step?.agent).toBe(1);
    expect(step?.row).toBe(UNITS_ROW);
  });

  it("moves down from an agent's last row into the next agent's first row, at the nearest item", () => {
    const step = navigateAcross("down", cursor(1, UNITS_ROW, 1), agents, BASE);
    expect(step?.agent).toBe(2);
    expect(step?.row).toBe(levelRowId(1));
    expect(step?.item.selection).toEqual({ kind: "node", id: "n2" });
  });

  it("moves up into the previous agent's last row", () => {
    const step = navigateAcross("up", { agent: 2, row: levelRowId(1), selection: { kind: "node", id: "n2" } }, agents, BASE);
    expect(step?.agent).toBe(1);
    expect(step?.row).toBe(UNITS_ROW);
  });

  it("stays within an agent for the moves that do not leave it, and stops at the ends", () => {
    expect(navigateAcross("right", cursor(1, UNITS_ROW, 0), agents, BASE)?.item.selection).toMatchObject({ i0: 1 });
    expect(navigateAcross("right", cursor(1, UNITS_ROW, 1), agents, BASE)).toBeNull();
    expect(navigateAcross("up", { agent: 1, row: levelRowId(1), selection: { kind: "node", id: "n1" } }, agents, BASE)).toBeNull();
  });

  it("skips an agent with nothing to land on", () => {
    expect(navigateAcross("down", cursor(2, UNITS_ROW, 0), agents, BASE)).toBeNull();
  });

  it("does not take the id of another agent's node for a relative", () => {
    // Agent 1's block names parent "n1", and agent 2 has a node of that id far from it: position decides.
    const x = data({ nodes: [node("n1", 1, 0, 400)], units: [{ ...unit(0, 300, 400), parent: "n1" }] });
    const y = data({ nodes: [node("n1", 1, 0, 100), node("n2", 1, 300, 400)] });
    const shared = buildSharedAxisMap(
      [
        { owner: 1, units: x.units },
        { owner: 2, units: y.units },
      ],
      BASE,
      "time",
    );
    const pair = [agent(1, x, shared), agent(2, y, shared)];
    const step = navigateAcross("down", cursor(1, UNITS_ROW, 0), pair, BASE);
    expect(step?.agent).toBe(2);
    expect(step?.item.selection).toEqual({ kind: "node", id: "n2" });
  });
});
