"use client";

// The arrows between agents: one canvas laid over the whole chart, redrawn once per animation frame
// after each render. It draws only; the pointer events stay with the rows below (the page asks `hit`
// which arrow is under the pointer). Where a row's track is on the page is read from the DOM, since the
// rows are laid out by CSS.

import { useEffect, useRef, type RefObject } from "react";

import { bucketOf, clusterToMax, curveOf, curvePoint, endTangent, hitLink, LINK_COLORS, type Arrow, type Cluster, type Curve, type ResolvedLink } from "../model/timeline-links";
import type { AxisMap, Viewport } from "../model/timeline-model";

const ROW_TESTID = { units: "run-timeline-row-units", user: "run-timeline-row-user", other: "run-timeline-row-other" } as const;
const HEAD_PX = 5;
/** How far from a curve a pointer still counts as on it. */
export const LINK_HIT_PX = 4;

/** `(clientX, clientY)` to the arrow under that point, or null. */
export type LinkHit = (clientX: number, clientY: number) => LinkHitResult | null;

interface Box {
  left: number;
  width: number;
  mid: number;
}

function boxOf(canvas: HTMLCanvasElement, end: ResolvedLink["from"]): Box | null {
  const scope = end.row === "units" ? `[data-testid="agent-view-agent-${end.agent}"]` : `[data-testid="agent-view-${end.row}"]`;
  const track = canvas.parentElement?.querySelector(`${scope} [data-testid="${ROW_TESTID[end.row]}"] [data-track]`);
  if (track == null) return null;
  const rect = track.getBoundingClientRect();
  const origin = canvas.getBoundingClientRect();
  return { left: rect.left - origin.left, width: rect.width, mid: rect.top - origin.top + rect.height / 2 };
}

/** The arrow under the pointer: its key (a link's own, or a merged group's) and every link it stands for. */
export interface LinkHitResult {
  key: string;
  members: readonly string[];
}

const BADGE_FONT_PX = 9;
const BADGE_AT = 0.2;
// One width for every arrow, merged or not; a hovered or selected one is a little thicker.
const WIDTH_PX = 1.25;

/** A cheap fingerprint of where the arrows are: equal when nothing moved, so the merge need not be searched again. */
function arrowsSignature(arrows: readonly Arrow[], max: number): string {
  let h0 = 0;
  let h1 = 0;
  for (const a of arrows) {
    h0 = (h0 * 31 + Math.round(a.x0 * 10) + a.key.length) % 1_000_000_007;
    h1 = (h1 * 37 + Math.round(a.x1 * 10) + a.bucket.length) % 1_000_000_007;
  }
  return `${max}|${arrows.length}|${h0}|${h1}`;
}

export function LinksCanvas({
  links,
  selectedKeys,
  hoverKeys,
  axis,
  view,
  maxArrows,
  hitRef,
}: {
  /** The arrows to draw (the kinds that are switched on). */
  links: readonly ResolvedLink[];
  /** The links selected / hovered: an arrow is lit when it stands for any of them. */
  selectedKeys: ReadonlySet<string>;
  hoverKeys: ReadonlySet<string>;
  axis: AxisMap;
  view: Viewport;
  /** The most arrows shown at once: close ones merge, as far as the viewport needs, to stay within it. */
  maxArrows: number;
  hitRef: RefObject<LinkHit>;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const cache = useRef<{ signature: string; clusters: Cluster[] } | null>(null);
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
      // A row's track is looked up in the page once per frame, not once per end.
      const boxes = new Map<string, ReturnType<typeof boxOf>>();
      const place = (end: ResolvedLink["from"]) => {
        const id = `${end.row}:${end.agent}`;
        if (!boxes.has(id)) boxes.set(id, boxOf(el, end));
        const box = boxes.get(id) ?? null;
        const frac = (axis.toU(end.ms) - shown.from) / span;
        return box === null || frac < 0 || frac > 1 ? null : { x: box.left + box.width * frac, y: box.mid };
      };
      const arrows: Arrow[] = [];
      const ys = new Map<string, [number, number]>();
      const byKey = new Map<string, ResolvedLink>();
      for (const l of links) {
        const a = place(l.from);
        const b = place(l.to);
        if (a === null || b === null) continue;
        arrows.push({ key: l.key, bucket: bucketOf(l), x0: a.x, x1: b.x });
        ys.set(l.key, [a.y, b.y]);
        byKey.set(l.key, l);
      }
      // Arrows close together on screen are drawn as one with a count, merged only as far as it takes to
      // stay within the limit; zooming in leaves fewer to merge, so they fall apart.
      // The search runs again only when the arrows moved (a zoom, a pan, a resize); hovering and
      // selecting repaint over the same result.
      const signature = arrowsSignature(arrows, maxArrows);
      if (cache.current?.signature !== signature) {
        cache.current = { signature, clusters: clusterToMax(arrows, maxArrows, rect.width * 2).clusters };
      }
      const clusters = cache.current.clusters;
      const curves: Curve[] = [];
      const sizes = new Map<string, number>();
      const kinds = new Map<string, ResolvedLink["kind"]>();
      const members = new Map<string, readonly string[]>();
      for (const c of clusters) {
        const [y0, y1] = ys.get(c.members[0]) ?? [0, 0];
        curves.push(curveOf(c.key, c.x0, y0, c.x1, y1));
        sizes.set(c.key, c.members.length);
        members.set(c.key, c.members);
        const kind = byKey.get(c.members[0])?.kind;
        if (kind !== undefined) kinds.set(c.key, kind);
      }
      const litOf = (key: string): 0 | 1 | 2 => {
        const keys = members.get(key) ?? [];
        return keys.some((k) => selectedKeys.has(k)) ? 2 : keys.some((k) => hoverKeys.has(k)) ? 1 : 0;
      };
      // The hovered and the selected arrow go on top of the rest.
      const order = [...curves].sort((p, q) => litOf(p.key) - litOf(q.key));
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (const c of order) {
        const kind = kinds.get(c.key);
        if (kind === undefined) continue;
        const lit = litOf(c.key);
        const count = sizes.get(c.key) ?? 1;
        const color = LINK_COLORS[kind];
        ctx.globalAlpha = lit > 0 ? 1 : selectedKeys.size > 0 ? 0.25 : 0.55;
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = lit === 2 ? WIDTH_PX + 1.25 : lit === 1 ? WIDTH_PX + 0.75 : WIDTH_PX;
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
        if (count > 1) {
          // The count, in a badge on the arrow just after it leaves its start row, clear of the rows between.
          const mid = curvePoint(c, BADGE_AT);
          const label = String(count);
          ctx.font = `600 ${BADGE_FONT_PX}px sans-serif`;
          const w = Math.max(ctx.measureText(label).width + 6, 14);
          ctx.globalAlpha = 1;
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.roundRect(mid.x - w / 2, mid.y - 7, w, 14, 7);
          ctx.fill();
          ctx.fillStyle = "#ffffff";
          ctx.fillText(label, mid.x, mid.y);
        }
      }
      ctx.globalAlpha = 1;
      hitRef.current = (clientX, clientY) => {
        const now = el.getBoundingClientRect();
        const key = hitLink(curves, clientX - now.left, clientY - now.top, LINK_HIT_PX);
        return key === null ? null : { key, members: members.get(key) ?? [key] };
      };
    });
    return () => cancelAnimationFrame(frame);
  });
  return <canvas ref={canvas} aria-hidden="true" data-testid="run-timeline-links-canvas" className="pointer-events-none absolute inset-0 z-10 size-full" />;
}
