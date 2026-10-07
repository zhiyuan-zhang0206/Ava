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
});

function renderRows(data: Partial<RunTimelineResponse>, selection: Selection | null = null) {
  const onSelect = vi.fn();
  render(
    <RunTimelineRows
      data={{ nodes: [], units: [], events: [], ...data } as RunTimelineResponse}
      base={BASE}
      view={BASE}
      onView={vi.fn()}
      selection={selection}
      onSelect={onSelect}
      onDrill={vi.fn()}
    />,
  );
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
