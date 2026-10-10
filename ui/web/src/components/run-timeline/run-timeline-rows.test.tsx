import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render as rtlRender, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunTimelineNode, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import { RunTimelineRows } from "./run-timeline-rows";
import type { UnitHeights } from "./canvas/run-timeline-paint";
import { LINK_KINDS } from "./model/timeline-links";
import { ALL_ROWS } from "./model/timeline-nav";
import { clickAt, drawn, leave, mockCanvas, paintFrame, pointAt } from "./canvas/run-timeline-test-canvas";
import type { Selection } from "./model/timeline-model";

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

/** A block; with `total` a request has read it (own tokens 5, `total` the context through it). */
const unit = (kind: RunTimelineUnit["kind"], i0: number, from: number, to: number, total: number | null = null): RunTimelineUnit => ({
  kind,
  i0,
  i1: i0,
  start: at(from),
  end: at(to),
  source: null,
  inbound_id: null,
  preview: kind,
  parent: null,
  context_tokens: total === null ? null : 5,
  generation_tokens: null,
  estimated: null,
  session: 0,
  context_total: total,
  request: null,
});

const ENTRIES = (data: Partial<RunTimelineResponse>) => [
  { id: 42, status: "loaded" as const, data: { nodes: [], units: [], ...data } as RunTimelineResponse },
];

function renderRows(data: Partial<RunTimelineResponse>, selection: Selection | null = null, heights: UnitHeights = "equal") {
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
      unitHeights={heights}
      onRemove={null}
      onRetry={vi.fn()}
      links={[]}
      showUser
      onShowUser={vi.fn()}
      showOther
      onShowOther={vi.fn()}
      linkKinds={new Set(LINK_KINDS)}
      onToggleLinkKind={vi.fn()}
      linkKey={null}
      onSelectLink={vi.fn()}
    />,
  );
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

  it("frames the primary bar strongly, and its block in the other row lightly", async () => {
    const units = [unit("text", 0, 0, 100, 5), unit("text", 1, 100, 200, 10)];
    renderRows({ units }, { kind: "unit", i0: 1, i1: 1, unitKind: "text" });
    await paintFrame();
    // The cursor is taken to be in the Messages row: the block is framed strongly, its bar lightly.
    expect(strokes("units", 2)).toHaveLength(1);
    expect(strokes("input", 2)).toHaveLength(0);
    expect(linkedFrames("input")).toHaveLength(1);
    const [frame] = linkedFrames("input");
    const bar = fills("input")[1];
    expect(frame.x).toBeLessThan(bar.x);
    expect(frame.x + frame.w).toBeGreaterThan(bar.x + bar.w);
  });

  it("hugs the bar: the frame is the bar's own width plus the frame room, never wider", async () => {
    renderRows({ units: [unit("text", 0, 100, 100, 5)] }, { kind: "unit", i0: 0, i1: 0, unitKind: "text" });
    await paintFrame();
    const [bar] = fills("units");
    const [frame] = strokes("units", 2);
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

describe("RunTimelineRows Messages heights", () => {
  const sized = [unit("text", 0, 0, 100, 10), unit("text", 1, 200, 300, 40), unit("text", 2, 400, 500, 90)];
  const withTokens = (tokens: number[]) => sized.map((u, i) => ({ ...u, context_tokens: tokens[i] }));

  it("draws every block the same height by default, from the bottom", async () => {
    renderRows({ units: withTokens([100, 400, 900]) });
    await paintFrame();
    const heights = fills("units").map((d) => d.h);
    expect(new Set(heights).size).toBe(1);
  });

  it("draws a block at a floor plus the rest of the height by the square root of its tokens, bottom-aligned", async () => {
    renderRows({ units: withTokens([1, 400, 900]) }, null, "tokens");
    await paintFrame();
    const [small, mid, big] = fills("units");
    // The row's drawing area is 32 px and the floor 6 px: 6 + 26 * sqrt(tokens / largest).
    expect(big.h).toBeCloseTo(32);
    expect(mid.h).toBeCloseTo(6 + 26 * (20 / 30));
    expect(small.h).toBeCloseTo(6 + 26 * (1 / 30));
    for (const d of [small, mid]) expect(d.y + d.h).toBeCloseTo(big.y + big.h);
  });

  it("keeps blocks of 10, 100 and 300 tokens apart next to a 20k one", async () => {
    renderRows({ units: withTokens([10, 100, 300]).map((u, i) => ({ ...u, context_tokens: [10, 100, 300][i] })).concat([{ ...unit("text", 3, 600, 700, 90), context_tokens: 21_000 }]) }, null, "tokens");
    await paintFrame();
    const [a, b, c, big] = fills("units");
    expect(a.h).toBeGreaterThanOrEqual(6);
    expect(b.h - a.h).toBeGreaterThan(1);
    expect(c.h - b.h).toBeGreaterThan(1);
    expect(big.h).toBeCloseTo(32);
  });

  it("clicks a small block anywhere in the row's height, and frames what is drawn", async () => {
    const onSelect = renderRows({ units: withTokens([1, 400, 900]) }, { kind: "unit", i0: 0, i1: 0, unitKind: "text" }, "tokens");
    await paintFrame();
    const [small, , big] = fills("units");
    // The hit-test reads x only: the whole height of the row answers, not just the drawn part.
    clickAt("units", small.x + small.w / 2);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "unit", i0: 0, i1: 0, unitKind: "text" });
    const [frame] = strokes("units", 2);
    expect(frame.y).toBeGreaterThan(big.y - 4);
    expect(frame.h).toBeLessThan(big.h);
  });
});

describe("RunTimelineRows context size row", () => {
  it("draws the context through each block, scaled to the largest, sessions in alternating colors", async () => {
    const units = [{ ...unit("text", 0, 100, 300, 1000), session: 0 }, { ...unit("text", 1, 500, 700, 2000), session: 1 }];
    renderRows({ units });
    await paintFrame();
    const [first, second] = fills("input");
    expect(first.h / second.h).toBeCloseTo(0.5);
    expect(first.color).toContain("#3b82f6");
    expect(second.color).toContain("#f59e0b");
    expect(screen.getByTestId("run-timeline-row-context")).toBeTruthy();
    expect(screen.queryByTestId("run-timeline-row-added")).toBeNull();
  });

  it("has the same blocks as the Messages row, to the pixel: same count, same x, same width", async () => {
    const units = [
      unit("inbound", 0, 0, 100, 5),
      unit("thinking", 1, 100, 500, 10),
      unit("call", 1, 500, 500, 12),
      unit("output", 2, 500, 800, 40),
      unit("inbound", 3, 900, 900, 50),
    ];
    renderRows({ units });
    await paintFrame();
    const messages = fills("units");
    const context = fills("input");
    expect(context).toHaveLength(messages.length);
    expect(context.map((d) => [d.x, d.w])).toEqual(messages.map((d) => [d.x, d.w]));
  });

  it("keeps that in a crowd of instants and short blocks", async () => {
    const units = Array.from({ length: 60 }, (_, i) => unit("text", i, i * 15, i * 15 + (i % 2 === 0 ? 0 : 6), 10 + i));
    renderRows({ units });
    await paintFrame();
    expect(fills("input").map((d) => [d.x, d.w])).toEqual(fills("units").map((d) => [d.x, d.w]));
  });

  it("draws an instant block as wide as the minimum, from its time, in both rows", async () => {
    renderRows({ units: [unit("inbound", 0, 400, 400, 5)] });
    await paintFrame();
    for (const row of ["units", "input"]) expect(fills(row).map((d) => [d.x, d.w])).toEqual([[400, 3]]);
  });

  it("paints overlapping instants once per pixel column, never layered", async () => {
    renderRows({ units: [unit("inbound", 0, 400, 400, 5), unit("inbound", 1, 401, 401, 10), unit("inbound", 2, 402, 402, 15)] });
    await paintFrame();
    for (const row of ["units", "input"]) {
      const boxes = fills(row);
      boxes.forEach((d, i) => boxes.slice(i + 1).forEach((e) => expect(d.x + d.w <= e.x || e.x + e.w <= d.x).toBe(true)));
      // Three 3 px items starting 1 px apart cover 5 px, each column once.
      expect(boxes.reduce((sum, d) => sum + d.w, 0)).toBe(5);
    }
  });

  it("has no Context size row for blocks no request has read", () => {
    renderRows({ units: [unit("text", 0, 0, 100)] });
    expect(screen.queryByTestId("run-timeline-row-context")).toBeNull();
  });
});

describe("RunTimelineRows keyboard and context bars", () => {
  const units = [unit("inbound", 0, 0, 100, 10), unit("thinking", 1, 100, 500, 20), unit("thinking", 2, 600, 900, 30)];

  it("selects the block when its bar is clicked", async () => {
    const onSelect = renderRows({ units });
    await paintFrame();
    const [, second] = fills("input");
    clickAt("input", second.x + second.w / 2);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "unit", i0: 1, i1: 1, unitKind: "thinking" });
  });

  it("keeps the session color on the selected bar, frames it and dims the others", async () => {
    renderRows({ units: [units[0], { ...units[1], session: 1 }] }, { kind: "unit", i0: 0, i1: 0, unitKind: "inbound" });
    await paintFrame();
    const [first, second] = fills("input");
    expect(first.color).toContain("#3b82f6");
    expect(first.color).not.toContain("50%");
    expect(second.color).toContain("50%");
  });

  it("frames only the visible part of a bar that starts left of the track", async () => {
    renderRows({ units: [unit("text", 0, -300, 400, 5)] }, { kind: "unit", i0: 0, i1: 0, unitKind: "text" });
    await paintFrame();
    const [bar] = fills("input");
    const frames = strokes("input", 1).filter((d) => d.dash.length > 0);
    expect(frames).toHaveLength(1);
    expect(frames[0].x + frames[0].w).toBeCloseTo(bar.x + bar.w + 2.5);
  });

  it("outlines the nodes over a selected block as ancestors", async () => {
    const covered = [{ ...unit("text", 1, 0, 100, 5), parent: "a" }];
    renderRows(
      { units: covered, nodes: [node("a", 0, 100, "p"), { ...node("p", 0, 100), level: 2 }] },
      { kind: "unit", i0: 1, i1: 1, unitKind: "text" },
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
    renderRows({ units });
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
