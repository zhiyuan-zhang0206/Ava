// Draws the rows of the run timeline on a canvas. One fill per pixel column and one block per wide
// item, every color opaque (a translucent look is mixed into the color, never layered), every edge on
// the device pixel grid, so the look is the same at any zoom. No React.

import { firstLine, tokenFits, tokenLabel, unitColor, unitKey, type Highlight, matchesHighlight } from "./timeline-model";
import { snap, type Cell, type Place, type RowLayout } from "./timeline-canvas-model";
import { isSelected, type Hover, type Selection } from "./timeline-model";
import { requestLit } from "./timeline-model";
import type { RunTimelineRequest } from "@/lib/contracts/types";

/** Resolves a CSS color (a variable, a `color-mix`) to an opaque canvas color. */
export type Resolve = (css: string) => string;

/** What the page lights, shared by every row. */
export interface PaintState {
  selection: Selection | null;
  hover: Hover | null;
  /** The ids of the nodes a selection lights as ancestors, and what a hover softly lights. */
  chain: ReadonlySet<string>;
  lit: { nodeIds: ReadonlySet<string>; unitKeys: ReadonlySet<string> };
  highlight: Highlight | null;
  selectedRequest: RunTimelineRequest | undefined;
}

export interface PaintCtx {
  ctx: CanvasRenderingContext2D;
  resolve: Resolve;
  width: number;
  height: number;
  dpr: number;
  font: string;
  /** The track's own background, which dimmed colors are mixed toward. */
  trackBg: string;
}

const FOREGROUND = "var(--foreground)";
const ACCENT = "var(--primary)";
// The selection frame: a stroke this wide, kept this far off the item.
const FRAME_PX = 2;
const FRAME_GAP_PX = 1;
const FRAME_ROOM_PX = FRAME_PX + FRAME_GAP_PX;
// What the items outside the selection keep of their color.
const DIM_SHARE = 0.5;
const FADE_SHARE = 0.12;
const BAR_ROOM_PX = 4;
const BAR_MIN_PX = 3;
const BLOCK_RADIUS = 4;
const UNIT_RADIUS = 2;
const UNIT_INSET = 4;
const TEXT_PAD = 4;
// A node keeps at least this much width for its summary before its token count is shown.
const NODE_LABEL_MIN_PX = 24;
const MIN_LABEL_PX = 28;
const NODE_LABEL_CHARS = 80;

function mix(color: string, share: number, into: string): string {
  return `color-mix(in srgb, ${color} ${Math.round(share * 100)}%, ${into})`;
}

function fillBox(p: PaintCtx, x0: number, x1: number, y0: number, y1: number, css: string, radius = 0) {
  const { ctx, dpr } = p;
  const left = snap(x0, dpr);
  const width = Math.max(snap(x1, dpr) - left, 1 / dpr);
  ctx.fillStyle = p.resolve(css);
  if (radius > 0 && width > 2 * radius) {
    ctx.beginPath();
    ctx.roundRect(left, y0, width, y1 - y0, radius);
    ctx.fill();
  } else ctx.fillRect(left, y0, width, y1 - y0);
}

function outline(p: PaintCtx, x0: number, x1: number, y0: number, y1: number, css: string, lineWidth: number, radius: number) {
  const { ctx, dpr } = p;
  const left = snap(x0, dpr) + lineWidth / 2;
  const right = snap(x1, dpr) - lineWidth / 2;
  ctx.strokeStyle = p.resolve(css);
  ctx.lineWidth = lineWidth;
  ctx.beginPath();
  if (right - left > 2 * radius) ctx.roundRect(left, y0 + lineWidth / 2, right - left, y1 - y0 - lineWidth, radius);
  else ctx.rect(left, y0 + lineWidth / 2, Math.max(right - left, 0), y1 - y0 - lineWidth);
  ctx.stroke();
}

function fitText(ctx: CanvasRenderingContext2D, text: string, maxPx: number): string {
  if (maxPx <= 0) return "";
  if (ctx.measureText(text).width <= maxPx) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ctx.measureText(`${text.slice(0, mid)}…`).width <= maxPx) lo = mid;
    else hi = mid - 1;
  }
  return lo === 0 ? "" : `${text.slice(0, lo)}…`;
}

/** Draws `place`'s text inside its box: the label left, the token count right, each only when it fits. */
function drawTexts(p: PaintCtx, place: Place, y: number, label: string | null, tokens: string | null, color: string) {
  const { ctx } = p;
  const width = place.x1 - place.x0;
  ctx.save();
  ctx.beginPath();
  ctx.rect(place.x0, 0, width, p.height);
  ctx.clip();
  ctx.textBaseline = "middle";
  ctx.fillStyle = p.resolve(color);
  let room = width - 2 * TEXT_PAD;
  if (tokens !== null && tokenFits(tokens, width, label === null ? 0 : NODE_LABEL_MIN_PX)) {
    ctx.font = `9px ${p.font}`;
    ctx.textAlign = "right";
    ctx.fillText(tokens, place.x1 - TEXT_PAD / 2, y);
    room -= ctx.measureText(tokens).width + TEXT_PAD;
  }
  if (label !== null && width >= MIN_LABEL_PX) {
    ctx.font = `10px ${p.font}`;
    ctx.textAlign = "left";
    ctx.fillText(fitText(ctx, label, room), place.x0 + TEXT_PAD, y);
  }
  ctx.restore();
}

/** What a row is decorated with besides its items: the selection's frame, and the line that marks a selection too narrow to see. */
export interface RowDeco {
  /** The box (px) the selection frame goes around in this row: the whole selected batch, one frame. */
  frame: { left: number; width: number } | null;
  /** x (px) of the thin line that marks a selection narrower than the frame's minimum; null when the selection is wide enough to see. */
  lineX: number | null;
}

/** The frame around a row's selection, and the hairline: one stroke in the accent, never a fill. */
function paintDeco(p: PaintCtx, deco: RowDeco, top: number, bottom: number) {
  if (deco.lineX !== null) {
    fillBox(p, deco.lineX - 0.5, deco.lineX + 0.5, 0, p.height, mix(FOREGROUND, 0.18, p.trackBg));
  }
  if (deco.frame !== null) {
    const { left, width } = deco.frame;
    outline(p, left - FRAME_ROOM_PX, left + width + FRAME_ROOM_PX, top - FRAME_ROOM_PX, bottom + FRAME_ROOM_PX, ACCENT, FRAME_PX, BLOCK_RADIUS);
  }
}

interface BlockState {
  picked: boolean;
  ancestor: boolean;
  hoverLight: boolean;
  hovered: boolean;
  faded: boolean;
  dim: boolean;
}

function tone(p: PaintCtx, s: { faded: boolean; dim: boolean }, color: string): string {
  if (s.faded) return mix(color, FADE_SHARE, p.trackBg);
  return s.dim ? mix(color, DIM_SHARE, p.trackBg) : color;
}

/** Level rows: one block per node, summary text and token count when wide enough; the hairlines of narrow nodes on top. */
export function paintNodes(p: PaintCtx, layout: RowLayout, state: PaintState, deco: RowDeco) {
  const top = FRAME_ROOM_PX;
  const bottom = p.height - FRAME_ROOM_PX;
  const stateOf = (key: string): BlockState => {
    const node = layout.items.get(key)?.node;
    const id = node?.id ?? "";
    const picked = state.selection?.kind === "node" && state.selection.id === id;
    const ancestor = !picked && state.chain.has(id);
    const hoverLight = !picked && !ancestor && state.lit.nodeIds.has(id);
    const hovered = state.hover?.kind === "node" && state.hover.id === id;
    const faded = state.highlight !== null && !picked;
    return { picked, ancestor, hoverLight, hovered, faded, dim: state.selection !== null && !picked && !ancestor && !hoverLight };
  };
  const base = (s: BlockState) => mix(ACCENT, s.ancestor ? 0.3 : 0.2, "var(--card)");
  for (const place of layout.wide) {
    const s = stateOf(place.key);
    const node = layout.items.get(place.key)?.node;
    fillBox(p, place.x0, place.x1, top, bottom, tone(p, s, base(s)), BLOCK_RADIUS);
    outline(p, place.x0, place.x1, top, bottom, tone(p, s, "var(--border)"), 1, BLOCK_RADIUS);
    if (s.ancestor) outline(p, place.x0, place.x1, top, bottom, mix(ACCENT, 0.6, "var(--card)"), 1, BLOCK_RADIUS);
    else if (s.hoverLight) outline(p, place.x0, place.x1, top, bottom, mix(ACCENT, 0.35, "var(--card)"), 1, BLOCK_RADIUS);
    if (node !== undefined) {
      drawTexts(p, place, p.height / 2, firstLine(node.summary, NODE_LABEL_CHARS), tokenLabel(node.context_tokens, node.estimated), tone(p, s, FOREGROUND));
    }
  }
  for (const cell of layout.cells) {
    const s = stateOf(cell.key);
    fillBox(p, cell.x0, cell.x1, top, bottom, tone(p, s, mix(ACCENT, s.ancestor || s.hoverLight ? 0.6 : 0.4, "var(--card)")));
  }
  paintDeco(p, deco, top, bottom);
}

/** The Messages row: one colored block per message unit. */
export function paintUnits(p: PaintCtx, layout: RowLayout, state: PaintState, deco: RowDeco) {
  const y1 = p.height - UNIT_INSET;
  const stateOf = (key: string) => {
    const unit = layout.items.get(key)?.unit;
    if (unit === undefined) return null;
    const candidate: Selection = { kind: "unit", i0: unit.i0, i1: unit.i1, unitKind: unit.kind };
    const picked =
      isSelected(state.selection, candidate) ||
      (state.selection?.kind === "request" && state.selectedRequest !== undefined && coveredBy(state.selectedRequest, unit.i0));
    const hovered = state.hover?.kind === "unit" && isSelected(state.hover, candidate);
    const hoverLight = hovered || state.lit.unitKeys.has(unitKey(unit));
    const matched = state.highlight !== null && matchesHighlight(unit, state.highlight);
    const faded = state.highlight !== null && !matched && !picked;
    return { unit, picked, hovered, hoverLight, faded, dim: state.highlight === null && state.selection !== null && !picked && !hoverLight };
  };
  for (const place of layout.wide) {
    const s = stateOf(place.key);
    if (s === null) continue;
    fillBox(p, place.x0, place.x1, UNIT_INSET, y1, tone(p, s, unitColor(s.unit)), UNIT_RADIUS);
    if (s.hoverLight && !s.picked) outline(p, place.x0, place.x1, UNIT_INSET, y1, mix(ACCENT, 0.35, "var(--card)"), 1, UNIT_RADIUS);
    drawTexts(p, place, UNIT_INSET + 5, null, tokenLabel(s.unit.context_tokens, s.unit.estimated), "#3f3f46");
  }
  for (const cell of layout.cells) {
    const s = stateOf(cell.key);
    if (s === null) continue;
    // A hairline lit by a hover is lifted toward the foreground a little; a selected one keeps its color.
    const color = s.hoverLight && !s.picked ? mix(FOREGROUND, 0.3, unitColor(s.unit)) : unitColor(s.unit);
    fillBox(p, cell.x0, cell.x1, UNIT_INSET, y1, tone(p, s, color));
  }
  paintDeco(p, deco, UNIT_INSET, y1);
}

function coveredBy(request: Pick<RunTimelineRequest, "added_from" | "added_to">, i0: number): boolean {
  return request.added_from <= i0 && i0 < request.added_to;
}

const BLUE = "#3b82f6";
const AMBER = "#f59e0b";

/** A context row: one bar per request as tall as its value, sessions alternating in color. */
export function paintBars(p: PaintCtx, layout: RowLayout, top: number, added: boolean, state: PaintState, deco: RowDeco) {
  if (!(top > 0)) return;
  const area = p.height - 2 * BAR_ROOM_PX;
  const bottom = p.height - BAR_ROOM_PX;
  const stateOf = (key: string) => {
    const request = layout.items.get(key)?.request;
    if (request === undefined) return null;
    const lit = requestLit(request, state.selection, state.hover);
    const height = Math.max(((layout.values?.get(key) ?? 0) / top) * area, BAR_MIN_PX);
    return { request, selected: lit.selected, hovered: lit.hovered, height };
  };
  const colorOf = (s: NonNullable<ReturnType<typeof stateOf>>) => {
    const hue = s.request.session % 2 === 0 ? BLUE : AMBER;
    const solid = mix(hue, s.request.session % 2 === 0 ? 0.7 : 0.8, "var(--card)");
    const own = added && s.request.added_estimated ? mix(solid, 0.6, p.trackBg) : solid;
    return state.selection !== null && !s.selected && !s.hovered ? mix(own, DIM_SHARE, p.trackBg) : own;
  };
  let tallest = 0;
  for (const key of layout.values?.keys() ?? []) {
    const s = stateOf(key);
    if (s?.selected === true) tallest = Math.max(tallest, s.height);
  }
  const draw = (x0: number, x1: number, key: string, ring: boolean) => {
    const s = stateOf(key);
    if (s === null) return;
    fillBox(p, x0, x1, bottom - s.height, bottom, colorOf(s));
    if (ring && s.hovered && !s.selected) outline(p, x0, x1, bottom - s.height, bottom, mix(ACCENT, 0.35, "var(--card)"), 1, 0);
  };
  for (const place of layout.wide) draw(place.x0, place.x1, place.key, true);
  for (const cell of layout.cells as readonly Cell[]) draw(cell.x0, cell.x1, cell.key, false);
  paintDeco(p, deco, bottom - Math.max(tallest, BAR_MIN_PX), bottom);
}
