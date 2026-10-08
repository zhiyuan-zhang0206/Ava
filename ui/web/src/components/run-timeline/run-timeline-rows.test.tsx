import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunTimelineNode, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import { RunTimelineRows } from "./run-timeline-rows";
import type { Selection } from "./timeline-model";

afterEach(cleanup);

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
  usage: { calls: 0, input: 0, cache_read: 0, output: 0 },
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

const span = (el: HTMLElement) => {
  const left = parseFloat(el.style.left);
  return { left, right: left + parseFloat(el.style.width) };
};

describe("RunTimelineRows instant blocks", () => {
  it("draws adjacent empty nodes as markers that stay clickable and do not cover the next node", () => {
    const onSelect = renderRows({ nodes: [node("a", 100, 100), node("b", 100, 100), node("c", 100, 400)] });
    const nodes = screen.getAllByTestId("run-timeline-node");
    expect(nodes.map((el) => el.hasAttribute("data-marker"))).toEqual([true, true, false]);
    const bodies = nodes.filter((el) => !el.hasAttribute("data-marker"));
    expect(span(bodies[0])).toEqual({ left: 100, right: 400 });
    const [a, b] = nodes;
    expect(parseFloat(a.style.top)).not.toBe(parseFloat(b.style.top));
    fireEvent.click(a);
    fireEvent.click(b);
    expect(onSelect.mock.calls).toEqual([
      [{ kind: "node", id: "a" }],
      [{ kind: "node", id: "b" }],
    ]);
  });

  it("keeps a call point from covering the output block that follows it", () => {
    const onSelect = renderRows({ units: [unit("call", 1, 500, 500), unit("output", 2, 500, 800)] });
    const [call, out] = screen.getAllByTestId("run-timeline-unit");
    expect(call.hasAttribute("data-marker")).toBe(true);
    expect(out.hasAttribute("data-marker")).toBe(false);
    expect(span(out)).toEqual({ left: 500, right: 800 });
    fireEvent.click(call);
    expect(onSelect).toHaveBeenCalledWith({ kind: "unit", i0: 1, i1: 1, unitKind: "call" });
  });

  it("shows selection and ancestor highlight on markers", () => {
    renderRows(
      { nodes: [node("p", 0, 100), node("a", 100, 100, "p"), node("c", 100, 400)] },
      { kind: "node", id: "a" },
    );
    const marker = screen.getAllByTestId("run-timeline-node").find((el) => el.dataset.nodeId === "a");
    expect(marker?.dataset.highlight).toBe("self");
    expect(marker?.querySelector("[data-testid=run-timeline-marker-line]")).toBeTruthy();
  });

  it("gives a run of points each their own clickable marker", () => {
    const onSelect = renderRows({
      units: [unit("call", 1, 200, 200), unit("call", 2, 200, 200), unit("call", 3, 200, 200), unit("output", 4, 200, 300)],
    });
    const all = screen.getAllByTestId("run-timeline-unit");
    const markers = all.filter((el) => el.hasAttribute("data-marker"));
    expect(markers).toHaveLength(3);
    all.forEach((el) => fireEvent.click(el));
    expect(onSelect).toHaveBeenCalledTimes(4);
  });
});

describe("RunTimelineRows hybrid axis", () => {
  const tokens = (u: RunTimelineUnit, n: number): RunTimelineUnit => ({ ...u, context_tokens: n });

  it("starts on the time axis and sizes blocks by tokens once switched to hybrid", () => {
    const units = [tokens(unit("text", 0, 0, 100), 1000), tokens(unit("text", 1, 100, 110), 3000)];
    renderRows({ units }, null, false);
    const toggle = screen.getByTestId("run-timeline-axis-mode");
    expect(toggle.dataset.mode).toBe("time");
    const [ta, tb] = screen.getAllByTestId("run-timeline-unit");
    expect(span(ta)).toEqual({ left: 0, right: 100 });
    expect(span(tb)).toEqual({ left: 100, right: 110 });
    fireEvent.click(toggle);
    expect(toggle.dataset.mode).toBe("hybrid");
    const [a, b] = screen.getAllByTestId("run-timeline-unit");
    expect(parseFloat(b.style.width) / parseFloat(a.style.width)).toBeCloseTo(3, 0);
  });

  it("puts a node over the blocks it covers", () => {
    const units = [tokens(unit("text", 0, 0, 100), 1000), tokens(unit("text", 1, 600, 700), 1000), tokens(unit("text", 2, 900, 950), 1000)];
    renderRows({ units, nodes: [{ ...node("n", 0, 1000), span_start: 1, span_end: 2 }] }, null, true);
    const [, second, third] = screen.getAllByTestId("run-timeline-unit");
    const [covering] = screen.getAllByTestId("run-timeline-node");
    expect(parseFloat(covering.style.left)).toBeCloseTo(parseFloat(second.style.left));
    expect(span(covering).right).toBeCloseTo(span(third).right);
  });
});

describe("RunTimelineRows context rows", () => {
  it("draws the absolute and the added context as two rows, each scaled to its own largest (the added one by square root)", () => {
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
    renderRows({ requests: [request(1, 100, 1000, 1000, true), request(2, 500, 2000, 100, false)] });
    const absolute = screen.getAllByTestId("run-timeline-request");
    const added = screen.getAllByTestId("run-timeline-added");
    expect(absolute).toHaveLength(2);
    expect(added).toHaveLength(2);
    const height = (el: HTMLElement) => parseFloat(el.querySelector<HTMLElement>("span[aria-hidden]")!.style.height);
    expect(height(absolute[0]) / height(absolute[1])).toBeCloseTo(0.5);
    // Square-root scale: 1000 vs 100 tokens is a height ratio of sqrt(10), the largest fills the area.
    expect(height(added[0]) / height(added[1])).toBeCloseTo(Math.sqrt(10));
    expect(height(added[0])).toBeCloseTo(height(absolute[1]));
    expect(added[0].hasAttribute("data-estimated")).toBe(true);
    expect(added[1].hasAttribute("data-estimated")).toBe(false);
    expect(screen.getByTestId("run-timeline-row-added")).toBeTruthy();
    expect(screen.getByTestId("run-timeline-row-context")).toBeTruthy();
    expect(added[1].style.left).toBe(absolute[1].style.left);
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

  it("selects the request when a bar is clicked", () => {
    const onSelect = renderRows({ units, requests: [request(1, 100), request(2, 600)] });
    fireEvent.click(screen.getAllByTestId("run-timeline-request")[1]);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "request", idx: 2 });
    fireEvent.click(screen.getAllByTestId("run-timeline-added")[0]);
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "request", idx: 1 });
  });

  it("lines a bar up with the blocks it read, less a pixel each side, on the same x as the Messages row", () => {
    // Request 2 first read message 1 only (100-500 ms): 1 px per ms on the 1000 px track.
    renderRows({ units, requests: [request(1, 100), request(2, 600)] });
    const [, bar] = screen.getAllByTestId("run-timeline-request");
    const block = screen.getAllByTestId("run-timeline-unit")[1];
    expect(parseFloat(bar.style.left)).toBeCloseTo(parseFloat(block.style.left) + 1);
    expect(parseFloat(bar.style.width)).toBeCloseTo(parseFloat(block.style.width) - 2);
    expect(screen.getAllByTestId("run-timeline-added")[1].style.left).toBe(bar.style.left);
  });

  it("selects every block a request read together with its bar, and lights them on hover", () => {
    renderRows(
      { units, requests: [request(1, 100), { ...request(3, 900), added_from: 1, added_to: 3 }] },
      { kind: "request", idx: 3 },
    );
    const blocks = screen.getAllByTestId("run-timeline-unit");
    expect(blocks.map((el) => el.getAttribute("data-highlight"))).toEqual(["none", "self", "self"]);
    const [first, second] = screen.getAllByTestId("run-timeline-request");
    expect(second.hasAttribute("data-selected")).toBe(true);
    expect(first.hasAttribute("data-selected")).toBe(false);
    fireEvent.mouseEnter(first);
    expect(screen.getAllByTestId("run-timeline-unit")[0].getAttribute("data-hover")).toBe("lit");
  });

  it("keeps the session color on the selected request bar and rings it", () => {
    renderRows({ units, requests: [request(1, 100), request(2, 600)] }, { kind: "request", idx: 1 });
    const [first, second] = screen.getAllByTestId("run-timeline-request").map((bar) => bar.querySelector("span[aria-hidden]")!);
    expect(first.className).toContain("bg-blue-500");
    expect(first.className).not.toContain("bg-foreground");
    expect(first.className).toContain("ring-2 ring-foreground");
    expect(second.className).not.toContain("ring-2");
  });

  it("lights the bar that read a selected block", () => {
    renderRows({ units, requests: [request(1, 100), request(2, 600)] }, { kind: "unit", i0: 1, i1: 1, unitKind: "thinking" });
    const [first, second] = screen.getAllByTestId("run-timeline-request");
    expect(second.hasAttribute("data-selected")).toBe(true);
    expect(first.hasAttribute("data-selected")).toBe(false);
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

  it("starts at the leftmost item in view when nothing is selected", () => {
    const onSelect = renderRows({ units });
    fireEvent.keyDown(window, { key: "ArrowDown" });
    expect(onSelect).toHaveBeenLastCalledWith({ kind: "unit", i0: 0, i1: 0, unitKind: "inbound" });
  });
});

describe("RunTimelineRows tokens and keyboard", () => {
  const counted = (u: RunTimelineUnit, n: number, estimated: boolean): RunTimelineUnit => ({
    ...u,
    context_tokens: n,
    estimated,
  });

  it("shows a node's and a block's tokens at their right end, ~ for an estimate, and drops them when narrow", () => {
    renderRows({
      units: [counted(unit("text", 0, 0, 500), 1500, true), counted(unit("text", 1, 500, 505), 20, false)],
      nodes: [
        { ...node("wide", 0, 600), context_tokens: 2300, estimated: false },
        { ...node("thin", 600, 610), context_tokens: 9, estimated: true },
      ],
    });
    const nodeTokens = screen.getAllByTestId("run-timeline-node").map((el) => el.querySelector("[data-testid=run-timeline-tokens]")?.textContent ?? null);
    expect(nodeTokens).toEqual(["2.3k", null]);
    const unitTokens = screen.getAllByTestId("run-timeline-unit").map((el) => el.querySelector("[data-testid=run-timeline-tokens]")?.textContent ?? null);
    expect(unitTokens).toEqual(["~1.5k", null]);
  });

  it("lights the nodes over the blocks of a selected request, like a selected block", () => {
    const units = [{ ...unit("text", 1, 0, 100), parent: "a" }];
    const request = { idx: 2, ts: at(100), session: 0, input_tokens: 5, output_tokens: 1, added_tokens: 1, added_estimated: false, added_from: 0, added_to: 2 };
    renderRows({ units, nodes: [node("a", 0, 100, "p"), { ...node("p", 0, 100), level: 2 }], requests: [request] }, { kind: "request", idx: 2 });
    const marks = screen.getAllByTestId("run-timeline-node").map((el) => [el.dataset.nodeId, el.dataset.highlight]);
    expect(marks).toEqual([["p", "ancestor"], ["a", "ancestor"]]);
  });

  it("an arrow key drops the focus and hover echo of the clicked bar", () => {
    const request = { idx: 1, ts: at(100), session: 0, input_tokens: 5, output_tokens: 1, added_tokens: 1, added_estimated: false, added_from: 0, added_to: 1 };
    renderRows({ units: [unit("text", 0, 0, 100)], requests: [request] });
    const bar = screen.getByTestId("run-timeline-request");
    bar.focus();
    fireEvent.focus(bar);
    expect(bar.querySelector("span")?.className).toContain("ring-1");
    fireEvent.keyDown(bar, { key: "ArrowUp" });
    expect(document.activeElement).not.toBe(bar);
    expect(bar.querySelector("span")?.className).not.toContain("ring-foreground/40");
  });
});
