// Draws the rows of the run timeline on a canvas. One fill per pixel column and one block per wide
// item, every color opaque (a translucent look is mixed into the color, never layered), every edge on
// the device pixel grid, so the look is the same at any zoom. No React.

import { firstLine, unitColor, unitKey, type Highlight, matchesHighlight } from "./timeline-model";
import { snap, type Cell, type Place, type RowLayout } from "./timeline-canvas-model";
import { messageLit, type Hover, type Selection } from "./timeline-model";

/** Resolves a CSS color (a variable, a `color-mix`) to an opaque canvas color. */
export type Resolve = (css: string) => string;

/** What the page lights, shared by every row (what the selection lights comes with each row's `RowDeco`). */
export interface PaintState {
  selection: Selection | null;
  hover: Hover | null;
  /** What a hover softly lights. */
  lit: { nodeIds: ReadonlySet<string>; unitKeys: ReadonlySet<string> };
  highlight: Highlight | null;
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

function outline(p: PaintCtx, x0: number, x1: number, y0: number, y1: number, css: string, lineWidth: number, radius: number, dash: number[] = []) {
  const { ctx, dpr } = p;
  const left = snap(x0, dpr) + lineWidth / 2;
  const right = snap(x1, dpr) - lineWidth / 2;
  ctx.strokeStyle = p.resolve(css);
  ctx.lineWidth = lineWidth;
  ctx.setLineDash(dash);
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

/** Draws a block's label inside its box when there is room for it. */
function drawLabel(p: PaintCtx, place: Place, y: number, label: string, color: string) {
  const { ctx } = p;
  const width = place.x1 - place.x0;
  if (width < MIN_LABEL_PX) return;
  ctx.save();
  ctx.beginPath();
  ctx.rect(place.x0, 0, width, p.height);
  ctx.clip();
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.fillStyle = p.resolve(color);
  ctx.font = `10px ${p.font}`;
  ctx.fillText(fitText(ctx, label, width - 2 * TEXT_PAD), place.x0 + TEXT_PAD, y);
  ctx.restore();
}

/** A box on a row's track, in pixels. */
export interface FrameBox {
  left: number;
  width: number;
}

/** What a row is decorated with besides its items: what the selection makes of its items, and the frames. */
export interface RowDeco {
  /** The key of the primary item when it is in this row, and the keys of the items linked to it. */
  primaryKey: string | null;
  linkedKeys: ReadonlySet<string>;
  /** The frames (px): around the primary item, and around the whole linked batch of this row, one frame each. */
  primary: FrameBox | null;
  linked: FrameBox | null;
  /** x (px) of the faint hairline that marks a primary item narrower than the frame's minimum; null otherwise. */
  lineX: number | null;
}

/** The frames around a row's selection (a thick accent one for the primary, a thin dashed one for what is linked) and the hairline. */
function paintDeco(p: PaintCtx, deco: RowDeco, top: number, bottom: number, topOf?: (keys: ReadonlySet<string>) => number) {
  if (deco.lineX !== null) fillBox(p, deco.lineX - 0.5, deco.lineX + 0.5, 0, p.height, mix(FOREGROUND, 0.18, p.trackBg));
  if (deco.linked !== null) {
    const y0 = topOf === undefined ? top : topOf(deco.linkedKeys);
    outline(p, deco.linked.left - FRAME_ROOM_PX, deco.linked.left + deco.linked.width + FRAME_ROOM_PX, y0 - FRAME_ROOM_PX, bottom + FRAME_ROOM_PX, mix(ACCENT, 0.6, "var(--card)"), 1, BLOCK_RADIUS, [3, 2]);
  }
  if (deco.primary !== null) {
    const y0 = topOf === undefined || deco.primaryKey === null ? top : topOf(new Set([deco.primaryKey]));
    outline(p, deco.primary.left - FRAME_ROOM_PX, deco.primary.left + deco.primary.width + FRAME_ROOM_PX, y0 - FRAME_ROOM_PX, bottom + FRAME_ROOM_PX, ACCENT, FRAME_PX, BLOCK_RADIUS);
  }
}

interface BlockState {
  primary: boolean;
  linked: boolean;
  hoverLight: boolean;
  hovered: boolean;
  faded: boolean;
  dim: boolean;
}

function tone(p: PaintCtx, s: { faded: boolean; dim: boolean }, color: string): string {
  if (s.faded) return mix(color, FADE_SHARE, p.trackBg);
  return s.dim ? mix(color, DIM_SHARE, p.trackBg) : color;
}

/** Level rows: one block per node with its summary when wide enough; the hairlines of narrow nodes on top. */
export function paintNodes(p: PaintCtx, layout: RowLayout, state: PaintState, deco: RowDeco) {
  const top = FRAME_ROOM_PX;
  const bottom = p.height - FRAME_ROOM_PX;
  const stateOf = (key: string): BlockState => {
    const id = layout.items.get(key)?.node?.id ?? "";
    const primary = deco.primaryKey === key;
    const linked = deco.linkedKeys.has(key);
    const hoverLight = !primary && !linked && state.lit.nodeIds.has(id);
    const hovered = state.hover?.kind === "node" && state.hover.id === id;
    const faded = state.highlight !== null && !primary;
    return { primary, linked, hoverLight, hovered, faded, dim: state.selection !== null && !primary && !linked && !hoverLight };
  };
  const base = mix(ACCENT, 0.2, "var(--card)");
  for (const place of layout.wide) {
    const s = stateOf(place.key);
    const node = layout.items.get(place.key)?.node;
    fillBox(p, place.x0, place.x1, top, bottom, tone(p, s, base), BLOCK_RADIUS);
    outline(p, place.x0, place.x1, top, bottom, tone(p, s, "var(--border)"), 1, BLOCK_RADIUS);
    if (s.hoverLight) outline(p, place.x0, place.x1, top, bottom, mix(ACCENT, 0.35, "var(--card)"), 1, BLOCK_RADIUS);
    if (node !== undefined) drawLabel(p, place, p.height / 2, firstLine(node.summary, NODE_LABEL_CHARS), tone(p, s, FOREGROUND));
  }
  for (const cell of layout.cells) {
    const s = stateOf(cell.key);
    fillBox(p, cell.x0, cell.x1, top, bottom, tone(p, s, mix(ACCENT, s.hoverLight ? 0.6 : 0.4, "var(--card)")));
  }
  paintDeco(p, deco, top, bottom);
}

/** The Messages row: one colored block per message unit. */
export function paintUnits(p: PaintCtx, layout: RowLayout, state: PaintState, deco: RowDeco) {
  const y1 = p.height - UNIT_INSET;
  const stateOf = (key: string) => {
    const unit = layout.items.get(key)?.unit;
    if (unit === undefined) return null;
    const primary = deco.primaryKey === key;
    const linked = deco.linkedKeys.has(key);
    const hovered = state.hover?.kind === "unit" && state.hover.i0 === unit.i0 && state.hover.i1 === unit.i1 && state.hover.unitKind === unit.kind;
    const hoverLight = !primary && !linked && (hovered || state.lit.unitKeys.has(unitKey(unit)));
    const matched = state.highlight !== null && matchesHighlight(unit, state.highlight);
    const faded = state.highlight !== null && !matched && !primary && !linked;
    return { unit, hovered, hoverLight, faded, dim: state.highlight === null && state.selection !== null && !primary && !linked && !hoverLight };
  };
  for (const place of layout.wide) {
    const s = stateOf(place.key);
    if (s === null) continue;
    fillBox(p, place.x0, place.x1, UNIT_INSET, y1, tone(p, s, unitColor(s.unit)), UNIT_RADIUS);
    if (s.hoverLight) outline(p, place.x0, place.x1, UNIT_INSET, y1, mix(ACCENT, 0.35, "var(--card)"), 1, UNIT_RADIUS);
  }
  for (const cell of layout.cells) {
    const s = stateOf(cell.key);
    if (s === null) continue;
    // A hairline lit by a hover is lifted toward the foreground a little.
    fillBox(p, cell.x0, cell.x1, UNIT_INSET, y1, tone(p, s, s.hoverLight ? mix(FOREGROUND, 0.3, unitColor(s.unit)) : unitColor(s.unit)));
  }
  paintDeco(p, deco, UNIT_INSET, y1);
}

const BLUE = "#3b82f6";
const AMBER = "#f59e0b";

/** A context row: one bar per message as tall as its value, sessions alternating in color. */
export function paintBars(p: PaintCtx, layout: RowLayout, top: number, added: boolean, state: PaintState, deco: RowDeco) {
  if (!(top > 0)) return;
  const area = p.height - 2 * BAR_ROOM_PX;
  const bottom = p.height - BAR_ROOM_PX;
  const heightOf = (key: string) => Math.max(((layout.values?.get(key) ?? 0) / top) * area, BAR_MIN_PX);
  const stateOf = (key: string) => {
    const message = layout.items.get(key)?.message;
    if (message === undefined) return null;
    const lifted = deco.primaryKey === key || deco.linkedKeys.has(key);
    return { message, lifted, hovered: messageLit(message.idx, state.hover), height: heightOf(key) };
  };
  const colorOf = (s: NonNullable<ReturnType<typeof stateOf>>) => {
    const hue = s.message.session % 2 === 0 ? BLUE : AMBER;
    const solid = mix(hue, s.message.session % 2 === 0 ? 0.7 : 0.8, "var(--card)");
    const own = added && s.message.estimated ? mix(solid, 0.6, p.trackBg) : solid;
    return state.selection !== null && !s.lifted && !s.hovered ? mix(own, DIM_SHARE, p.trackBg) : own;
  };
  const draw = (x0: number, x1: number, key: string, ring: boolean) => {
    const s = stateOf(key);
    if (s === null) return;
    fillBox(p, x0, x1, bottom - s.height, bottom, colorOf(s));
    if (ring && s.hovered && !s.lifted) outline(p, x0, x1, bottom - s.height, bottom, mix(ACCENT, 0.35, "var(--card)"), 1, 0);
  };
  for (const place of layout.wide) draw(place.x0, place.x1, place.key, true);
  for (const cell of layout.cells as readonly Cell[]) draw(cell.x0, cell.x1, cell.key, false);
  // The selected and linked bars go on top again, so a hairline of another message inside their range does not cut them.
  for (const place of layout.wide) if (deco.primaryKey === place.key || deco.linkedKeys.has(place.key)) draw(place.x0, place.x1, place.key, true);
  // A frame reaches the top of the tallest bar it surrounds, and no higher.
  const topOf = (keys: ReadonlySet<string>) => bottom - Math.max(BAR_MIN_PX, ...[...keys].map(heightOf));
  paintDeco(p, deco, bottom - BAR_MIN_PX, bottom, topOf);
}
