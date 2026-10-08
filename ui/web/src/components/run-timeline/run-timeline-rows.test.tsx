import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunTimelineNode, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import { RunTimelineRows } from "./run-timeline-rows";
import { clickAt, drawn, leave, mockCanvas, paintFrame, pointAt } from "./run-timeline-test-canvas";
import type { Selection } from "./timeline-model";

beforeEach(mockCanvas);
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const T0 = Date.parse("2026-10-04T12:00:00.000Z");
const at = (ms: number) => new Date(T0 + ms).toISOString();
const BASE = { from: T0, to: T0 + 1000 };

const node = (id: string, from: number, to: number, parent: string | null = null): RunTimelineNode => ({
  id,
  level: 1,
  parent,
  start: at(from),
  end: at(to),
  span_start: 0,
  span_end: 0,
  summary: `node ${id}`,
  usage: { calls: 0, input: 0, cache_read: 0, output: 0, cache_write: 0, cost_usd: 0, cost_calls: 0 },
  generation: null,
  context_tokens: null,
  estimated: null,
});

const unit = (kind: RunTimelineUnit["kind"], i0: number, from: number, to: number): RunTimelineUnit => ({
  kind,
  i0,
  i1: i0,
  start: at(from),
  end: at(to),
  source: null,
  preview: kind,
  parent: null,
  context_tokens: null, generation_tokens: null, estimated: null,
});

function renderRows(data: Partial<RunTimelineResponse>, selection: Selection | null = null, hybrid = false) {
  const onSelect = vi.fn();
  render(
    <RunTimelineRows
      data={{ nodes: [], units: [], events: [], requests: [], ...data } as RunTimelineResponse}
      base={BASE}
      view={BASE}
      onView={vi.fn()}
      selection={selection}
      onSelect={onSelect}
      highlight={null}
      onHighlight={vi.fn()}
    />,
  );
  // These cases are about positions on the plain time axis.
  if (hybrid) fireEvent.click(screen.getByTestId("run-timeline-axis-mode"));
  return onSelect;
}

const fills = (row: string) => drawn(row).filter((d) => d.op === "fill");
const strokes = (row: string, lineWidth?: number) =>
  drawn(row).filter((d) => d.op === "stroke" && (lineWidth === undefined || d.lineWidth === lineWidth));
const linkedFrames = (row: string) => drawn(row).filter((d) => d.op === "stroke" && d.dash.length > 0);
const texts = (row: string) => drawn(row).filter((d) => d.op === "text").map((d) => d.text);

describe("RunTimelineRows canvas rows", () => {
  it("draws rows on canvases, not a block element per item", async () => {
    renderRows({ nodes: [node("a", 0, 500)], units: [unit("text", 0, 0, 100)] });
    await paintFrame();
    expect(screen.queryAllByTestId("run-timeline-node")).toHaveLength(0);
    expect(screen.queryAllByTestId("run-timeline-unit")).toHaveLength(0);
    expect(fills("level-1")).toHaveLength(1);
    expect(fills("units")).toHaveLength(1);
  });

  it("selects the item under the pointer, hairlines included, and the block over the hairlines under it", async () => {
    const onSelect = renderRows({ nodes: [node("a", 100, 100), node("c", 200, 400)] });
    await paintFrame();
    clickAt("level-1", 100.5);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "node", id: "a" });
    clickAt("level-1", 300);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "node", id: "c" });
    onSelect.mockClear();
    clickAt("level-1", 600);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("draws a call point as a hairline over the output block that follows it, and still reaches both", async () => {
    const onSelect = renderRows({ units: [unit("call", 1, 500, 500), unit("output", 2, 500, 800)] });
    await paintFrame();
    expect(fills("units").map((d) => [d.x, d.w])).toEqual([[500, 300], [500, 1]]);
    clickAt("units", 500.5);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "unit", i0: 1, i1: 1, unitKind: "call" });
    clickAt("units", 650);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "unit", i0: 2, i1: 2, unitKind: "output" });
  });

  it("frames the selected node once, in the accent, and keeps its color", async () => {
    renderRows({ nodes: [node("p", 0, 100), node("a", 100, 100, "p"), node("c", 100, 400)] }, { kind: "node", id: "a" });
    await paintFrame();
    expect(strokes("level-1", 2)).toHaveLength(1);
    expect(strokes("level-1", 2)[0].color).toBe("var(--primary)");
    expect(screen.queryByTestId("run-timeline-selection-box")).toBeNull();
    expect(screen.getByTestId("run-timeline-selection-live").textContent).toContain("node a");
  });

  it("lights an ancestor with a lighter outline, and steps the unselected back", async () => {
    renderRows({ nodes: [{ ...node("p", 0, 500), level: 2 }, node("a", 0, 100, "p"), node("b", 200, 300, "p")] }, { kind: "node", id: "a" });
    await paintFrame();
    const ancestor = strokes("level-2", 1).filter((d) => d.color.includes("var(--primary) 60%"));
    expect(ancestor).toHaveLength(1);
    expect(strokes("level-2", 2)).toHaveLength(0);
    const [selected, other] = fills("level-1");
    expect(selected.color).not.toContain("50%");
    expect(other.color).toContain("50%");
    expect(fills("level-2")[0].color).not.toContain("50%");
  });

});

describe("RunTimelineRows narrow items", () => {
  it("paints a pixel column once however many items fall into it, and never gives a narrow one a border", async () => {
    const narrow = Array.from({ length: 10 }, (_, i) => node(`n${i}`, 100 + i * 0.2, 100.2 + i * 0.2));
    const onSelect = renderRows({ nodes: [...narrow, node("w", 400, 800)] });
    await paintFrame();
    const small = fills("level-1").filter((d) => d.radius === 0);
    expect(small.length).toBeLessThanOrEqual(2);
    small.forEach((d, i) => small.slice(i + 1).forEach((e) => expect(d.x + d.w <= e.x || e.x + e.w <= d.x).toBe(true)));
    const wide = fills("level-1").filter((d) => d.radius > 0);
    expect(wide).toHaveLength(1);
    expect(wide[0].w).toBeGreaterThanOrEqual(4);
    // The wide node has its 1 px border; the narrow ones draw none.
    expect(strokes("level-1", 1)).toHaveLength(1);
    clickAt("level-1", 100.5);
    expect(onSelect.mock.calls.at(-1)?.[0]).toMatchObject({ kind: "node" });
  });

  it("frames a selection narrower than 6 px at 6 px and marks it with a faint line in the tracks only", async () => {
    renderRows({ nodes: [node("a", 100, 100.5), node("b", 600, 900)] }, { kind: "node", id: "a" });
    await paintFrame();
    const frame = strokes("level-1", 2)[0];
    expect(frame.w).toBeGreaterThanOrEqual(6);
    const line = fills("level-1").filter((d) => d.w <= 1 && d.color.includes("var(--foreground) 18%"));
    expect(line).toHaveLength(1);
    expect(line[0].h).toBe(32);
  });

  it("draws no line when the selection is wide enough to see", async () => {
    renderRows({ nodes: [node("a", 100, 400)] }, { kind: "node", id: "a" });
    await paintFrame();
    expect(fills("level-1").filter((d) => d.color.includes("var(--foreground) 18%"))).toHaveLength(0);
  });

  it("frames the primary bar strongly and the batch it read, and the same request in the other row, lightly", async () => {
    const units = [unit("text", 0, 0, 100), unit("text", 1, 100, 200)];
    const request = { idx: 2, ts: at(200), session: 0, input_tokens: 5, output_tokens: 1, added_tokens: 1, added_estimated: false, added_from: 0, added_to: 2 };
    renderRows({ units, requests: [request] }, { kind: "request", idx: 2 });
    await paintFrame();
    expect(strokes("input", 2)).toHaveLength(1);
    expect(strokes("units", 2)).toHaveLength(0);
    expect(strokes("added", 2)).toHaveLength(0);
    // One light frame per row around the whole batch, not one per block.
    expect(linkedFrames("units")).toHaveLength(1);
    expect(linkedFrames("added")).toHaveLength(1);
    const [frame] = linkedFrames("units");
    expect(frame.x).toBeLessThan(fills("units")[0].x);
    expect(frame.x + frame.w).toBeGreaterThan(fills("units")[1].x + fills("units")[1].w);
  });

  it("hugs the bar: the frame is the bar's own width plus the gap and stroke, never the message range or a minimum", async () => {
    const thin = [unit("text", 0, 0, 100), unit("text", 1, 100, 102)];
    const request = { idx: 2, ts: at(102), session: 0, input_tokens: 5, output_tokens: 1, added_tokens: 1, added_estimated: false, added_from: 1, added_to: 2 };
    renderRows({ units: thin, requests: [request] }, { kind: "request", idx: 2 });
    await paintFrame();
    const [bar] = fills("input");
    const [frame] = strokes("input", 2);
    // The stroke's centre line sits 2 px outside the bar: its outer edge is 3 px off (1 px gap plus the 2 px stroke).
    expect(frame.x).toBeCloseTo(bar.x - 2);
    expect(frame.w).toBeCloseTo(bar.w + 4);
  });

});

describe("RunTimelineRows hybrid axis", () => {
  const tokens = (u: RunTimelineUnit, n: number): RunTimelineUnit => ({ ...u, context_tokens: n });

  it("starts on the time axis and sizes blocks by tokens once switched to hybrid", async () => {
    const units = [tokens(unit("text", 0, 0, 100), 1000), tokens(unit("text", 1, 100, 110), 3000)];
    renderRows({ units }, null, false);
    const toggle = screen.getByTestId("run-timeline-axis-mode");
    await paintFrame();
    expect(toggle.dataset.mode).toBe("time");
    expect(fills("units").map((d) => [d.x, d.x + d.w])).toEqual([[0, 100], [100, 110]]);
    fireEvent.click(toggle);
    await paintFrame();
    expect(toggle.dataset.mode).toBe("hybrid");
    const [a, b] = fills("units");
    expect(b.w / a.w).toBeCloseTo(3, 0);
  });

  it("puts a node over the blocks it covers", async () => {
    const units = [tokens(unit("text", 0, 0, 100), 1000), tokens(unit("text", 1, 600, 700), 1000), tokens(unit("text", 2, 900, 950), 1000)];
    renderRows({ units, nodes: [{ ...node("n", 0, 1000), span_start: 1, span_end: 2 }] }, null, true);
    await paintFrame();
    const [, second, third] = fills("units");
    const [covering] = fills("level-1");
    expect(covering.x).toBeCloseTo(second.x, 0);
    expect(covering.x + covering.w).toBeCloseTo(third.x + third.w, 0);
  });
});

describe("RunTimelineRows tokens", () => {
  const counted = (u: RunTimelineUnit, n: number, estimated: boolean): RunTimelineUnit => ({
    ...u,
    context_tokens: n,
    estimated,
  });

  it("shows no token count on a node or a block, only the node's summary", async () => {
    renderRows({
      units: [counted(unit("text", 0, 0, 500), 1500, true)],
      nodes: [{ ...node("wide", 0, 600), context_tokens: 2300, estimated: false }],
    });
    await paintFrame();
    expect(texts("level-1")).toEqual(["node wide"]);
    expect(texts("units")).toEqual([]);
  });

});

describe("RunTimelineRows context rows", () => {
  const request = (idx: number, ms: number, input: number, added: number, estimated: boolean) => ({
    idx,
    ts: at(ms),
    session: 0,
    input_tokens: input,
    output_tokens: 1,
    added_tokens: added,
    added_estimated: estimated,
    added_from: idx - 1,
    added_to: idx,
  });

  it("draws the absolute and the added context as two rows, each scaled to its own largest (the added one by square root)", async () => {
    renderRows({ requests: [request(1, 100, 1000, 1000, true), request(2, 500, 2000, 100, false)] });
    await paintFrame();
    const absolute = fills("input");
    const added = fills("added");
    expect(absolute).toHaveLength(2);
    expect(added).toHaveLength(2);
    expect(absolute[0].h / absolute[1].h).toBeCloseTo(0.5);
    expect(added[0].h / added[1].h).toBeCloseTo(Math.sqrt(10));
    expect(added[0].h).toBeCloseTo(absolute[1].h);
    // An estimated addition is drawn paler, in an opaque mix.
    expect(added[0].color).toContain("60%");
    expect(added[1].color).not.toContain("60%");
    expect(screen.getByTestId("run-timeline-row-added")).toBeTruthy();
    expect(screen.getByTestId("run-timeline-row-context")).toBeTruthy();
    expect(added[1].x).toBe(absolute[1].x);
  });
});

describe("RunTimelineRows keyboard and request bars", () => {
  const request = (idx: number, ms: number) => ({
    idx,
    ts: at(ms),
    session: 0,
    input_tokens: 100,
    output_tokens: 1,
    added_tokens: 10,
    added_estimated: false,
    added_from: idx - 1,
    added_to: idx,
  });
  const units = [unit("inbound", 0, 0, 100), unit("thinking", 1, 100, 500), unit("thinking", 2, 600, 900)];

  it("selects the request when a bar is clicked, in either context row", async () => {
    const onSelect = renderRows({ units, requests: [request(1, 100), request(2, 600)] });
    await paintFrame();
    const [, second] = fills("input");
    clickAt("input", second.x + second.w / 2);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "request", idx: 2 });
    const [first] = fills("added");
    clickAt("added", first.x + first.w / 2);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "request", idx: 1 });
  });

  it("lines a bar up with the blocks it read, less a pixel each side, on the same x as the Messages row", async () => {
    // Request 2 first read message 1 only (100-500 ms): 1 px per ms on the 1000 px track.
    renderRows({ units, requests: [request(1, 100), request(2, 600)] });
    await paintFrame();
    const [, bar] = fills("input");
    const block = fills("units")[1];
    expect(bar.x).toBeCloseTo(block.x + 1);
    expect(bar.w).toBeCloseTo(block.w - 2);
    expect(fills("added")[1].x).toBe(bar.x);
  });

  it("frames the batch a request read lightly, and lights it softly while its bar is hovered", async () => {
    renderRows(
      { units, requests: [request(1, 100), { ...request(3, 900), added_from: 1, added_to: 3 }] },
      { kind: "request", idx: 3 },
    );
    await paintFrame();
    expect(strokes("input", 2)).toHaveLength(1);
    expect(linkedFrames("units")).toHaveLength(1);
    expect(linkedFrames("added")).toHaveLength(1);
    const [first] = fills("input");
    pointAt("input", first.x + first.w / 2);
    await paintFrame();
    expect(strokes("input", 1).filter((d) => d.dash.length === 0).length).toBeGreaterThan(0);
  });

  it("keeps the session color on the selected request bar, frames it and dims the other", async () => {
    renderRows({ units, requests: [request(1, 100), request(2, 600)] }, { kind: "request", idx: 1 });
    await paintFrame();
    const [first, second] = fills("input");
    expect(first.color).toContain("#3b82f6");
    expect(first.color).not.toContain("50%");
    expect(second.color).toContain("50%");
    expect(strokes("input", 2)).toHaveLength(1);
  });

  it("frames only the visible part of a bar that starts left of the track", async () => {
    const early = [unit("text", 0, -300, 400)];
    const request = { idx: 1, ts: at(400), session: 0, input_tokens: 5, output_tokens: 1, added_tokens: 1, added_estimated: false, added_from: 0, added_to: 1 };
    renderRows({ units: early, requests: [request] }, { kind: "request", idx: 1 });
    await paintFrame();
    const [bar] = fills("input");
    const [frame] = strokes("input", 2);
    // The bar runs from before the track to 399 px; the frame's right edge is 3 px past that, as for any bar.
    expect(frame.x + frame.w).toBeCloseTo(bar.x + bar.w + 2);
  });

  it("draws the selected bar over a hairline of another request that falls inside its range", async () => {
    const wideUnits = [unit("text", 0, 0, 400), unit("text", 1, 400, 401), unit("text", 2, 401, 500)];
    // Request 1 read blocks 0-1 (0..401 ms) in one bar; request 2 read block 2 (a later, narrower bar) and the instant request 3 sits inside the first range in time.
    const requests = [
      { ...request(1, 0), added_from: 0, added_to: 2 },
      { ...request(2, 450), added_from: 2, added_to: 3 },
      { ...request(3, 100), added_from: 3, added_to: 4 },
    ];
    renderRows({ units: [...wideUnits, { ...unit("text", 3, 150, 151) }], requests }, { kind: "request", idx: 1 });
    await paintFrame();
    const atX = fills("input").filter((d) => d.x <= 150 && 150 <= d.x + d.w);
    expect(atX.length).toBeGreaterThan(1);
    // The last thing painted over x = 150 is the selected bar, in its full color.
    expect(atX.at(-1)?.color).toContain("#3b82f6");
    expect(atX.at(-1)?.color).not.toContain("50%");
  });

  it("frames the selected block strongly and the bar that read it lightly", async () => {
    renderRows({ units, requests: [request(1, 100), request(2, 600)] }, { kind: "unit", i0: 1, i1: 1, unitKind: "thinking" });
    await paintFrame();
    expect(strokes("units", 2)).toHaveLength(1);
    expect(strokes("input", 2)).toHaveLength(0);
    expect(linkedFrames("input")).toHaveLength(1);
    const [first, second] = fills("input");
    expect(second.color).not.toContain("50%");
    expect(first.color).toContain("50%");
  });

  it("outlines the nodes over the blocks of a selected request as ancestors, like a selected block", async () => {
    const covered = [{ ...unit("text", 1, 0, 100), parent: "a" }];
    const requestRead = { idx: 2, ts: at(100), session: 0, input_tokens: 5, output_tokens: 1, added_tokens: 1, added_estimated: false, added_from: 0, added_to: 2 };
    renderRows(
      { units: covered, nodes: [node("a", 0, 100, "p"), { ...node("p", 0, 100), level: 2 }], requests: [requestRead] },
      { kind: "request", idx: 2 },
    );
    await paintFrame();
    expect(strokes("level-1", 1).filter((d) => d.color.includes("var(--primary) 60%"))).toHaveLength(1);
    expect(strokes("level-2", 1).filter((d) => d.color.includes("var(--primary) 60%"))).toHaveLength(1);
  });

  it("moves the selection with the arrow keys and ignores them in an input", () => {
    const onSelect = renderRows({ units }, { kind: "unit", i0: 0, i1: 0, unitKind: "inbound" });
    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "unit", i0: 1, i1: 1, unitKind: "thinking" });
    const input = document.createElement("input");
    document.body.appendChild(input);
    onSelect.mockClear();
    fireEvent.keyDown(input, { key: "ArrowRight" });
    expect(onSelect).not.toHaveBeenCalled();
    input.remove();
  });

  it("an arrow key clears the hover echo of the pointer", async () => {
    renderRows({ units, requests: [request(1, 100)] });
    await paintFrame();
    const [bar] = fills("input");
    pointAt("input", bar.x + bar.w / 2);
    await paintFrame();
    expect(strokes("input", 1)).toHaveLength(1);
    fireEvent.keyDown(window, { key: "ArrowUp" });
    await paintFrame();
    expect(strokes("input", 1)).toHaveLength(0);
    leave("input");
  });
  it("starts at the leftmost item in view when nothing is selected", () => {
    const onSelect = renderRows({ units });
    fireEvent.keyDown(window, { key: "ArrowDown" });
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "unit", i0: 0, i1: 0, unitKind: "inbound" });
  });
});
