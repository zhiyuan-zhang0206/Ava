// Test helpers for the canvas rows: a recording 2D context (what each canvas drew in its last frame)
// and pointer events at an item's position. Test code only; never imported by the app.

import { fireEvent, screen } from "@testing-library/react";
import { act } from "react";
import { vi } from "vitest";

import type { RunTimelineResponse } from "@/lib/contracts/types";

import { layoutsFor } from "./timeline-canvas-model";
import { buildAxisMap, viewportOf } from "./timeline-model";

/** One thing a canvas drew: a fill, a stroke or a text, with the color and geometry it used. */
export interface Drawn {
  op: "fill" | "stroke" | "text";
  x: number;
  y: number;
  w: number;
  h: number;
  color: string;
  lineWidth: number;
  radius: number;
  text: string;
  align: string;
  /** The dash pattern of a stroke: empty for a solid line. */
  dash: number[];
}

const noop = () => undefined;

class RecordingContext {
  fillStyle = "";
  strokeStyle = "";
  lineWidth = 1;
  font = "";
  textAlign = "left";
  textBaseline = "alphabetic";
  dash: number[] = [];
  frame: Drawn[] = [];
  private path: { x: number; y: number; w: number; h: number; radius: number } | null = null;

  // The state calls a test has no use for: kept as no-ops so painting runs unchanged.
  setLineDash(dash: number[]) {
    this.dash = dash;
  }
  setTransform = noop;
  save = noop;
  restore = noop;
  clip = noop;
  beginPath() {
    this.path = null;
  }
  clearRect() {
    this.frame = [];
  }
  rect(x: number, y: number, w: number, h: number) {
    this.path = { x, y, w, h, radius: 0 };
  }
  roundRect(x: number, y: number, w: number, h: number, radius: number) {
    this.path = { x, y, w, h, radius };
  }
  measureText(text: string) {
    return { width: text.length * 5 };
  }
  private push(op: Drawn["op"], box: { x: number; y: number; w: number; h: number; radius: number }, color: string, text = "") {
    this.frame.push({ op, ...box, color, lineWidth: this.lineWidth, text, align: this.textAlign, dash: this.dash });
  }
  fillRect(x: number, y: number, w: number, h: number) {
    this.push("fill", { x, y, w, h, radius: 0 }, this.fillStyle);
  }
  fill() {
    if (this.path !== null) this.push("fill", this.path, this.fillStyle);
  }
  stroke() {
    if (this.path !== null) this.push("stroke", this.path, this.strokeStyle);
  }
  fillText(text: string, x: number, y: number) {
    this.push("text", { x, y, w: 0, h: 0, radius: 0 }, this.fillStyle, text);
  }
}

const contexts = new WeakMap<HTMLCanvasElement, RecordingContext>();

/** Makes every canvas hand out a recording context; call in `beforeEach`, and `vi.restoreAllMocks()` after. */
export function mockCanvas() {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(function (this: HTMLCanvasElement) {
    let ctx = contexts.get(this);
    if (ctx === undefined) {
      ctx = new RecordingContext();
      contexts.set(this, ctx);
    }
    return ctx as unknown as CanvasRenderingContext2D;
  } as never);
}

/** Lets the animation frame of the latest render run. */
export async function paintFrame() {
  await act(async () => {
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
  });
}

/** What the canvas of `row` drew in its last frame. */
export function drawn(row: string): Drawn[] {
  const el = screen.getByTestId<HTMLCanvasElement>(`run-timeline-canvas-${row}`);
  return contexts.get(el)?.frame ?? [];
}

/** Fills and strokes of a frame at about pixel `x` (the blocks, not the text). */
export function shapesAt(row: string, x: number): Drawn[] {
  return drawn(row).filter((d) => d.op !== "text" && d.x - 1 <= x && x <= d.x + d.w + 1);
}

/** The pixel at the middle of an item of a row, on the plain time axis over the response's window and a 1000 px track. */
export function itemX(data: RunTimelineResponse, row: string, key: string, trackPx = 1000): number {
  const base = viewportOf(data.window);
  const axis = buildAxisMap(data.units, base, "time");
  const layout = layoutsFor(data, axis, axis.viewU(base), trackPx).get(row);
  const place = layout?.wide.find((p) => p.key === key);
  if (place !== undefined) return (place.x0 + place.x1) / 2;
  const cell = layout?.cells.find((c) => c.key === key);
  if (cell === undefined) throw new Error(`no ${key} in ${row}`);
  return (cell.x0 + cell.x1) / 2;
}

export function pointAt(row: string, x: number) {
  fireEvent.pointerMove(screen.getByTestId(`run-timeline-canvas-${row}`), { clientX: x });
}

export function leave(row: string) {
  fireEvent.pointerLeave(screen.getByTestId(`run-timeline-canvas-${row}`));
}

export function clickAt(row: string, x: number) {
  fireEvent.click(screen.getByTestId(`run-timeline-canvas-${row}`), { clientX: x });
}

/** How an item of a row looks at pixel `x`: primary (inside the strong frame), linked (inside the light dashed frame), hovered, and whether a highlight faded it. */
export function look(row: string, x: number): { ring: "primary" | "linked" | "hover" | "none"; faded: boolean } {
  const shapes = shapesAt(row, x);
  const strength = (d: Drawn) => {
    if (d.op === "stroke") {
      if (d.color === "var(--primary)") return 3;
      if (d.dash.length > 0) return 2;
      if (d.color.includes("var(--primary) 35%")) return 1;
      return 0;
    }
    // A hairline too narrow for an outline shows a hover in its fill.
    return d.op === "fill" && d.color.includes("var(--foreground) 30%") ? 1 : 0;
  };
  const best = Math.max(0, ...shapes.map(strength));
  return {
    ring: (["none", "hover", "linked", "primary"] as const)[best],
    faded: shapes.some((d) => d.op === "fill" && d.color.includes("12%")),
  };
}
