import { describe, expect, it } from "vitest";

import type { RunTimelineNode, RunTimelineRequest, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import { navigateAcross, type ViewAgent } from "./agent-view-nav";
import { timeAxis } from "./timeline-model";
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

describe("navigateAcross", () => {
  const agent = (id: number, d: RunTimelineResponse, context: "both" | "off" = "off"): ViewAgent => ({
    id,
    data: d,
    rows: navRowIds(d, { levels: null, context }),
  });
  const d1 = data({ nodes: [node("n1", 1, 0, 400)], units: [unit(0, 0, 100), unit(1, 100, 200)] });
  const d2 = data({ nodes: [node("n2", 1, 0, 400)], units: [unit(0, 300, 400)] });
  const d3 = data({});
  const axis = timeAxis(BASE);
  const agents = [agent(1, d1), agent(2, d2), agent(3, d3)];
  const cursor = (id: number, row: string, key: number) => ({
    agent: id,
    row,
    selection: { kind: "unit" as const, i0: key, i1: key, unitKind: "text" as const },
  });

  it("starts at the first agent that has something in view", () => {
    const step = navigateAcross("right", null, agents, axis, BASE);
    expect(step?.agent).toBe(1);
    expect(step?.row).toBe(UNITS_ROW);
  });

  it("moves down from an agent's last row into the next agent's first row, at the nearest item", () => {
    const step = navigateAcross("down", cursor(1, UNITS_ROW, 1), agents, axis, BASE);
    expect(step?.agent).toBe(2);
    expect(step?.row).toBe(levelRowId(1));
    expect(step?.item.selection).toEqual({ kind: "node", id: "n2" });
  });

  it("moves up into the previous agent's last row", () => {
    const step = navigateAcross("up", { agent: 2, row: levelRowId(1), selection: { kind: "node", id: "n2" } }, agents, axis, BASE);
    expect(step?.agent).toBe(1);
    expect(step?.row).toBe(UNITS_ROW);
  });

  it("stays within an agent for the moves that do not leave it, and stops at the ends", () => {
    expect(navigateAcross("right", cursor(1, UNITS_ROW, 0), agents, axis, BASE)?.item.selection).toMatchObject({ i0: 1 });
    expect(navigateAcross("right", cursor(1, UNITS_ROW, 1), agents, axis, BASE)).toBeNull();
    expect(navigateAcross("up", { agent: 1, row: levelRowId(1), selection: { kind: "node", id: "n1" } }, agents, axis, BASE)).toBeNull();
  });

  it("skips an agent with nothing to land on", () => {
    expect(navigateAcross("down", cursor(2, UNITS_ROW, 0), agents, axis, BASE)).toBeNull();
  });

  it("does not take the id of another agent's node for a relative", () => {
    // Agent 1's block names parent "n1", and agent 2 has a node of that id far from it: position decides.
    const x = data({ nodes: [node("n1", 1, 0, 400)], units: [{ ...unit(0, 300, 400), parent: "n1" }] });
    const y = data({ nodes: [node("n1", 1, 0, 100), node("n2", 1, 300, 400)] });
    const pair = [agent(1, x), agent(2, y)];
    const step = navigateAcross("down", cursor(1, UNITS_ROW, 0), pair, axis, BASE);
    expect(step?.agent).toBe(2);
    expect(step?.item.selection).toEqual({ kind: "node", id: "n2" });
  });
});
