"use client";

// The arrows between agents: one canvas laid over the whole chart, redrawn once per animation frame
// after each render. It draws only; the pointer events stay with the rows below (the page asks `hit`
// which arrow is under the pointer). Where a row's track is on the page is read from the DOM, since the
// rows are laid out by CSS.

import { useEffect, useRef, type RefObject } from "react";

import { curveOf, endTangent, hitLink, LINK_COLORS, type Curve, type ResolvedLink } from "../model/timeline-links";
import type { AxisMap, Viewport } from "../model/timeline-model";

const ROW_TESTID = { units: "run-timeline-row-units", lifecycle: "run-timeline-row-lifecycle", other: "run-timeline-row-other" } as const;
const OTHER_GROUP = "agent-view-other-agents";
const HEAD_PX = 5;
/** How far from a curve a pointer still counts as on it. */
export const LINK_HIT_PX = 4;

/** `(clientX, clientY)` to the key of the arrow under that point, or null. */
export type LinkHit = (clientX: number, clientY: number) => string | null;

interface Box {
  left: number;
  width: number;
  mid: number;
}

function boxOf(canvas: HTMLCanvasElement, end: ResolvedLink["from"]): Box | null {
  const scope = end.row === "other" ? `[data-testid="${OTHER_GROUP}"]` : `[data-testid="agent-view-agent-${end.agent}"]`;
  const track = canvas.parentElement?.querySelector(`${scope} [data-testid="${ROW_TESTID[end.row]}"] [data-track]`);
  if (track == null) return null;
  const rect = track.getBoundingClientRect();
  const origin = canvas.getBoundingClientRect();
  return { left: rect.left - origin.left, width: rect.width, mid: rect.top - origin.top + rect.height / 2 };
}

export function LinksCanvas({
  links,
  selectedKey,
  hoverKey,
  axis,
  view,
  hitRef,
}: {
  /** The arrows to draw (the kinds that are switched on). */
  links: readonly ResolvedLink[];
  selectedKey: string | null;
  hoverKey: string | null;
  axis: AxisMap;
  view: Viewport;
  hitRef: RefObject<LinkHit>;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const el = canvas.current;
    const ctx = el?.getContext("2d") ?? null;
    if (el === null || ctx === null) return;
    const frame = requestAnimationFrame(() => {
      const rect = el.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const pixelW = Math.max(1, Math.round(rect.width * dpr));
      const pixelH = Math.max(1, Math.round(rect.height * dpr));
      if (el.width !== pixelW) el.width = pixelW;
      if (el.height !== pixelH) el.height = pixelH;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, rect.width, rect.height);
      const shown = axis.viewU(view);
      const span = Math.max(shown.to - shown.from, 1e-9);
      const place = (end: ResolvedLink["from"]) => {
        const box = boxOf(el, end);
        const frac = (axis.toU(end.ms) - shown.from) / span;
        return box === null || frac < 0 || frac > 1 ? null : { x: box.left + box.width * frac, y: box.mid };
      };
      const curves: Curve[] = [];
      for (const l of links) {
        const a = place(l.from);
        const b = place(l.to);
        if (a !== null && b !== null) curves.push(curveOf(l.key, a.x, a.y, b.x, b.y));
      }
      const byKey = new Map(links.map((l) => [l.key, l]));
      // The hovered and the selected arrow go on top of the rest.
      const order = [...curves].sort((p, q) => Number(p.key === selectedKey || p.key === hoverKey) - Number(q.key === selectedKey || q.key === hoverKey));
      for (const c of order) {
        const l = byKey.get(c.key);
        if (l === undefined) continue;
        const lit = c.key === selectedKey ? 2 : c.key === hoverKey ? 1 : 0;
        ctx.globalAlpha = lit > 0 ? 1 : selectedKey !== null ? 0.25 : 0.55;
        ctx.strokeStyle = LINK_COLORS[l.link.kind];
        ctx.fillStyle = ctx.strokeStyle;
        ctx.lineWidth = lit === 2 ? 2.5 : lit === 1 ? 2 : 1.25;
        ctx.beginPath();
        ctx.moveTo(c.x0, c.y0);
        ctx.bezierCurveTo(c.c1x, c.c1y, c.c2x, c.c2y, c.x1, c.y1);
        ctx.stroke();
        // The head points along the curve's tangent at its end.
        const tan = endTangent(c);
        const bx = c.x1 - tan.x * HEAD_PX;
        const by = c.y1 - tan.y * HEAD_PX;
        ctx.beginPath();
        ctx.moveTo(c.x1, c.y1);
        ctx.lineTo(bx - tan.y * HEAD_PX * 0.6, by + tan.x * HEAD_PX * 0.6);
        ctx.lineTo(bx + tan.y * HEAD_PX * 0.6, by - tan.x * HEAD_PX * 0.6);
        ctx.closePath();
        ctx.fill();
      }
      ctx.globalAlpha = 1;
      hitRef.current = (clientX, clientY) => {
        const now = el.getBoundingClientRect();
        return hitLink(curves, clientX - now.left, clientY - now.top, LINK_HIT_PX);
      };
    });
    return () => cancelAnimationFrame(frame);
  });
  return <canvas ref={canvas} aria-hidden="true" data-testid="run-timeline-links-canvas" className="pointer-events-none absolute inset-0 z-10 size-full" />;
}
