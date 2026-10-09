import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render as rtlRender, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunTimelineMessageBar, RunTimelineNode, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import { RunTimelineRows } from "./run-timeline-rows";
import { ALL_ROWS } from "./timeline-nav";
import { clickAt, drawn, leave, mockCanvas, paintFrame, pointAt } from "./run-timeline-test-canvas";
import type { Selection } from "./timeline-model";

vi.mock("@/lib/transport/api", () => ({
  api: { getAgentRoster: vi.fn(() => Promise.resolve({ agents: [], ancestors: [] })), getAgent: vi.fn() },
}));

function render(ui: React.ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

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

const ENTRIES = (data: Partial<RunTimelineResponse>) => [
  { id: 42, status: "loaded" as const, data: { nodes: [], units: [], events: [], messages: [], ...data } as RunTimelineResponse },
];

function renderRows(data: Partial<RunTimelineResponse>, selection: Selection | null = null) {
  const onSelect = vi.fn();
  render(
    <RunTimelineRows
      entries={ENTRIES(data)}
      base={BASE}
      view={BASE}
      onView={vi.fn()}
      selection={selection === null ? null : { agent: 42, selection }}
      onSelect={(picked) => void onSelect(picked.selection)}
      highlight={null}
      onHighlight={vi.fn()}
      options={ALL_ROWS}
      onRemove={null}
      onRetry={vi.fn()}
    />,
  );
  return onSelect;
}

const message = (
  idx: number,
  from: number,
  to: number,
  own: number,
  total: number,
  extra: Partial<RunTimelineMessageBar> = {},
): RunTimelineMessageBar => ({
  idx,
  start: at(from),
  end: at(to),
  session: 0,
  context_tokens: own,
  estimated: false,
  context_total: total,
  request: null,
  ...extra,
});

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
    clickAt("level-1", 101);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "node", id: "a" });
    clickAt("level-1", 300);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "node", id: "c" });
    onSelect.mockClear();
    clickAt("level-1", 600);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("draws a call point at its time, as wide as the minimum, over the output block that follows it, and still reaches both", async () => {
    const onSelect = renderRows({ units: [unit("call", 1, 500, 500), unit("output", 2, 500, 800)] });
    await paintFrame();
    expect(fills("units").map((d) => [d.x, d.w])).toEqual([[500, 300], [500, 3]]);
    clickAt("units", 501);
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
    // Every item is at least 3 px wide, so the ten overlap: each column is still painted by one of them.
    small.forEach((d, i) => small.slice(i + 1).forEach((e) => expect(d.x + d.w <= e.x || e.x + e.w <= d.x).toBe(true)));
    const wide = fills("level-1").filter((d) => d.radius > 0);
    expect(wide).toHaveLength(1);
    expect(wide[0].w).toBeGreaterThanOrEqual(4);
    // The wide node has its 1 px border; the narrow ones draw none.
    expect(strokes("level-1", 1)).toHaveLength(1);
    clickAt("level-1", 100.5);
    expect(onSelect.mock.calls.at(-1)?.[0]).toMatchObject({ kind: "node" });
  });

  it("frames an instant exactly as wide as it is drawn (3 px) and marks it with a faint line in the tracks only", async () => {
    renderRows({ nodes: [node("a", 100, 100), node("b", 600, 900)] }, { kind: "node", id: "a" });
    await paintFrame();
    const frame = strokes("level-1", 2)[0];
    const [bar] = fills("level-1").filter((d) => d.x < 200);
    expect(bar.w).toBe(3);
    expect(frame.x).toBeCloseTo(bar.x - 2);
    expect(frame.w).toBeCloseTo(bar.w + 4);
    const line = fills("level-1").filter((d) => d.w <= 1 && d.color.includes("var(--foreground) 18%"));
    expect(line).toHaveLength(1);
    expect(line[0].h).toBe(32);
  });

  it("draws no line when the selection is wide enough to see", async () => {
    renderRows({ nodes: [node("a", 100, 400)] }, { kind: "node", id: "a" });
    await paintFrame();
    expect(fills("level-1").filter((d) => d.color.includes("var(--foreground) 18%"))).toHaveLength(0);
  });

  it("frames the primary bar strongly, and its block and its twin bar lightly", async () => {
    const units = [unit("text", 0, 0, 100), unit("text", 1, 100, 200)];
    renderRows({ units, messages: [message(0, 0, 100, 5, 5), message(1, 100, 200, 5, 10)] }, { kind: "message", idx: 1 });
    await paintFrame();
    expect(strokes("input", 2)).toHaveLength(1);
    expect(strokes("units", 2)).toHaveLength(0);
    expect(strokes("added", 2)).toHaveLength(0);
    // One light frame per row: the block that shows the message, and the same message in the other context row.
    expect(linkedFrames("units")).toHaveLength(1);
    expect(linkedFrames("added")).toHaveLength(1);
    const [frame] = linkedFrames("units");
    const block = fills("units")[1];
    expect(frame.x).toBeLessThan(block.x);
    expect(frame.x + frame.w).toBeGreaterThan(block.x + block.w);
  });

  it("hugs the bar: the frame is the bar's own width plus the frame room, never wider", async () => {
    const instant = [unit("text", 0, 100, 100)];
    renderRows({ units: instant, messages: [message(0, 100, 100, 5, 5)] }, { kind: "message", idx: 0 });
    await paintFrame();
    const [bar] = fills("input");
    const [frame] = strokes("input", 2);
    expect(bar.w).toBe(3);
    // The stroke's centre line sits 2 px outside the bar: its outer edge is 3 px off.
    expect(frame.x).toBeCloseTo(bar.x - 2);
    expect(frame.w).toBeCloseTo(bar.w + 4);
  });

});

describe("RunTimelineRows time axis", () => {
  it("places blocks by time, whatever their tokens, and offers no other axis", async () => {
    const units = [{ ...unit("text", 0, 0, 100), context_tokens: 1000 }, { ...unit("text", 1, 100, 110), context_tokens: 3000 }];
    renderRows({ units });
    await paintFrame();
    expect(fills("units").map((d) => [d.x, d.x + d.w])).toEqual([[0, 100], [100, 110]]);
    expect(screen.queryByTestId("run-timeline-axis-mode")).toBeNull();
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
  it("draws the context through each message and the message's own weight as two rows, each scaled to its own largest (the added one by square root)", async () => {
    renderRows({
      units: [unit("text", 0, 100, 300), unit("text", 1, 500, 700)],
      messages: [message(0, 100, 300, 1000, 1000, { estimated: true }), message(1, 500, 700, 100, 2000)],
    });
    await paintFrame();
    const absolute = fills("input");
    const added = fills("added");
    expect(absolute).toHaveLength(2);
    expect(added).toHaveLength(2);
    expect(absolute[0].h / absolute[1].h).toBeCloseTo(0.5);
    expect(added[0].h / added[1].h).toBeCloseTo(Math.sqrt(10));
    expect(added[0].h).toBeCloseTo(absolute[1].h);
    // An estimated weight is drawn paler in the Added row, in an opaque mix.
    expect(added[0].color).toContain("60%");
    expect(added[1].color).not.toContain("60%");
    expect(screen.getByTestId("run-timeline-row-added")).toBeTruthy();
    expect(screen.getByTestId("run-timeline-row-context")).toBeTruthy();
    expect(added[1].x).toBe(absolute[1].x);
  });

  it("lines every bar up with the block that shows its message, to the pixel, in both rows", async () => {
    const units = [unit("inbound", 0, 0, 100), unit("thinking", 1, 100, 500), unit("call", 1, 500, 500)];
    renderRows({ units, messages: [message(0, 0, 100, 5, 5), message(1, 100, 500, 5, 10)] });
    await paintFrame();
    const [first, second] = fills("units");
    const [firstBar, secondBar] = fills("input");
    expect([firstBar.x, firstBar.w]).toEqual([first.x, first.w]);
    expect([secondBar.x, secondBar.w]).toEqual([second.x, second.w]);
    expect(fills("added").map((d) => [d.x, d.w])).toEqual(fills("input").map((d) => [d.x, d.w]));
  });

  it("draws an instant message as wide as the minimum, from its time, in all three rows", async () => {
    renderRows({ units: [unit("inbound", 0, 400, 400)], messages: [message(0, 400, 400, 5, 5)] });
    await paintFrame();
    for (const row of ["units", "input", "added"]) expect(fills(row).map((d) => [d.x, d.w])).toEqual([[400, 3]]);
  });

  it("paints overlapping instants once per pixel column, never layered", async () => {
    const units = [unit("inbound", 0, 400, 400), unit("inbound", 1, 401, 401), unit("inbound", 2, 402, 402)];
    renderRows({
      units,
      messages: [message(0, 400, 400, 5, 5), message(1, 401, 401, 5, 10), message(2, 402, 402, 5, 15)],
    });
    await paintFrame();
    for (const row of ["units", "input", "added"]) {
      const boxes = fills(row);
      boxes.forEach((d, i) => boxes.slice(i + 1).forEach((e) => expect(d.x + d.w <= e.x || e.x + e.w <= d.x).toBe(true)));
      // Three 3 px items starting 1 px apart cover 5 px, each column once.
      expect(boxes.reduce((sum, d) => sum + d.w, 0)).toBe(5);
    }
  });
});

describe("RunTimelineRows keyboard and message bars", () => {
  const units = [unit("inbound", 0, 0, 100), unit("thinking", 1, 100, 500), unit("thinking", 2, 600, 900)];
  const messages = [message(0, 0, 100, 10, 10), message(1, 100, 500, 10, 20), message(2, 600, 900, 10, 30)];

  it("selects the message when a bar is clicked, in either context row", async () => {
    const onSelect = renderRows({ units, messages });
    await paintFrame();
    const [, second] = fills("input");
    clickAt("input", second.x + second.w / 2);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "message", idx: 1 });
    const [first] = fills("added");
    clickAt("added", first.x + first.w / 2);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "message", idx: 0 });
  });

  it("keeps the session color on the selected message's bar, frames it and dims the others", async () => {
    renderRows({ units, messages: [message(0, 0, 100, 10, 10), message(1, 100, 500, 10, 20, { session: 1 })] }, { kind: "message", idx: 0 });
    await paintFrame();
    const [first, second] = fills("input");
    expect(first.color).toContain("#3b82f6");
    expect(first.color).not.toContain("50%");
    expect(second.color).toContain("50%");
    expect(strokes("input", 2)).toHaveLength(1);
  });

  it("alternates the bar color with the session, a compaction starting over", async () => {
    renderRows({ units, messages: [message(0, 0, 100, 10, 10), message(1, 100, 500, 10, 20, { session: 1 })] });
    await paintFrame();
    const [first, second] = fills("input");
    expect(first.color).toContain("#3b82f6");
    expect(second.color).toContain("#f59e0b");
  });

  it("frames only the visible part of a bar that starts left of the track", async () => {
    renderRows({ units: [unit("text", 0, -300, 400)], messages: [message(0, -300, 400, 5, 5)] }, { kind: "message", idx: 0 });
    await paintFrame();
    const [bar] = fills("input");
    const [frame] = strokes("input", 2);
    // The bar runs from before the track to 400 px; the frame's right edge is 3 px past that, as for any bar.
    expect(frame.x + frame.w).toBeCloseTo(bar.x + bar.w + 2);
  });

  it("frames the selected block strongly and the bars of the messages it shows lightly", async () => {
    renderRows({ units, messages }, { kind: "unit", i0: 1, i1: 1, unitKind: "thinking" });
    await paintFrame();
    expect(strokes("units", 2)).toHaveLength(1);
    expect(strokes("input", 2)).toHaveLength(0);
    expect(linkedFrames("input")).toHaveLength(1);
    const [first, second] = fills("input");
    expect(second.color).not.toContain("50%");
    expect(first.color).toContain("50%");
  });

  it("outlines the nodes over the blocks of a selected message as ancestors, like a selected block", async () => {
    const covered = [{ ...unit("text", 1, 0, 100), parent: "a" }];
    renderRows(
      { units: covered, nodes: [node("a", 0, 100, "p"), { ...node("p", 0, 100), level: 2 }], messages: [message(1, 0, 100, 5, 5)] },
      { kind: "message", idx: 1 },
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
    renderRows({ units, messages });
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
