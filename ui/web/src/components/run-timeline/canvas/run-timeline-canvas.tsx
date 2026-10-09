"use client";

// One row's canvas: sized to its track in device pixels, redrawn once per animation frame after
// each render (so a burst of zoom or pan events costs one paint), with a pointer layer that turns a
// position into the item under it (`hitTest`). The canvas is decorative: every item stays reachable
// by the arrow keys and is read out through the page's live region.

import { useEffect, useMemo, useRef } from "react";

import { buildHitIndex, hitTest, type RowLayout } from "../model/timeline-canvas-model";
import type { PaintCtx, Resolve } from "./run-timeline-paint";

let probe: HTMLElement | null = null;
const resolved = new Map<string, string>();
let watching = false;

/** The opaque canvas color of a CSS color expression (variables and `color-mix` included), cached until the theme changes. */
const resolveColor: Resolve = (css) => {
  const known = resolved.get(css);
  if (known !== undefined) return known;
  if (typeof document === "undefined") return css;
  if (!watching) {
    watching = true;
    const clear = () => resolved.clear();
    new MutationObserver(clear).observe(document.documentElement, { attributes: true });
    if (typeof window.matchMedia === "function") window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", clear);
  }
  if (probe === null) {
    probe = document.createElement("span");
    probe.setAttribute("aria-hidden", "true");
    probe.style.display = "none";
    document.body.appendChild(probe);
  }
  probe.style.backgroundColor = "";
  probe.style.backgroundColor = css;
  const color = getComputedStyle(probe).backgroundColor || css;
  resolved.set(css, color);
  return color;
};

export function TrackCanvas({
  width,
  height,
  layout,
  paint,
  testId,
  onHit,
  onChoose,
}: {
  /** The track's width in CSS pixels. */
  width: number;
  height: number;
  layout: RowLayout;
  paint: (p: PaintCtx) => void;
  testId: string;
  /** The item under the pointer (its key) or null; fired when it changes. */
  onHit: (key: string | null) => void;
  onChoose: (key: string) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const index = useMemo(() => buildHitIndex(layout), [layout]);
  const last = useRef<string | null>(null);

  useEffect(() => {
    const el = canvas.current;
    const ctx = el?.getContext("2d") ?? null;
    if (el === null || ctx === null) return;
    const frame = requestAnimationFrame(() => {
      const dpr = window.devicePixelRatio || 1;
      const pixelW = Math.max(1, Math.round(width * dpr));
      const pixelH = Math.max(1, Math.round(height * dpr));
      if (el.width !== pixelW) el.width = pixelW;
      if (el.height !== pixelH) el.height = pixelH;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      const style = getComputedStyle(el);
      paint({
        ctx,
        resolve: resolveColor,
        width,
        height,
        dpr,
        font: style.fontFamily || "sans-serif",
        trackBg: "color-mix(in srgb, var(--muted) 40%, var(--card))",
      });
    });
    return () => cancelAnimationFrame(frame);
  });

  const keyAt = (event: React.PointerEvent | React.MouseEvent) =>
    hitTest(index, event.clientX - event.currentTarget.getBoundingClientRect().left);
  return (
    <canvas
      ref={canvas}
      aria-hidden="true"
      data-testid={testId}
      className="absolute inset-0 size-full"
      onPointerMove={(event) => {
        const key = keyAt(event);
        if (key === last.current) return;
        last.current = key;
        event.currentTarget.style.cursor = key === null ? "" : "pointer";
        onHit(key);
      }}
      onPointerLeave={() => {
        last.current = null;
        onHit(null);
      }}
      onClick={(event) => {
        const key = keyAt(event);
        if (key !== null) onChoose(key);
      }}
    />
  );
}
