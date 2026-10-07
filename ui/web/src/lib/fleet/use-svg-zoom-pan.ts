"use client";

import { select } from "d3-selection";
import { zoom as d3Zoom, zoomIdentity, type D3ZoomEvent, type ZoomBehavior, type ZoomTransform } from "d3-zoom";
import { useCallback, useRef, useState } from "react";

export interface SvgBounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export interface SvgViewport {
  minX: number;
  minY: number;
  w: number;
  h: number;
}

type SvgExtent = [[number, number], [number, number]];

// Non-functional floor prevents a zero-area transform. The upper bound must
// remain open (user ruling 2026-08-25: zoom must never be capped).
const ZOOM_MIN = 0.001;

export function fitSvgBoxTransform(
  bounds: SvgBounds,
  viewport: SvgViewport,
  padding: number,
  fitRatio: number,
): ZoomTransform {
  const minX = bounds.minX - padding;
  const minY = bounds.minY - padding;
  const maxX = bounds.maxX + padding;
  const maxY = bounds.maxY + padding;
  const boxW = maxX - minX || 1;
  const boxH = maxY - minY || 1;
  const boxCx = (minX + maxX) / 2;
  const boxCy = (minY + maxY) / 2;
  const fitScale = Math.min(viewport.w / boxW, viewport.h / boxH) * fitRatio;
  const scale = Math.max(ZOOM_MIN, fitScale);
  const tx = viewport.minX + viewport.w / 2 - boxCx * scale;
  const ty = viewport.minY + viewport.h / 2 - boxCy * scale;
  return zoomIdentity.translate(tx, ty).scale(scale);
}

export function useSvgZoomPan(initialExtent: SvgExtent) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const zoomRef = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const extentRef = useRef<SvgExtent>(initialExtent);
  const [transform, setTransform] = useState<ZoomTransform>(zoomIdentity);
  // Programmatic transforms animate; wheel/drag follow the pointer 1:1.
  const [animateZoom, setAnimateZoom] = useState(false);

  const setExtent = useCallback((viewport: SvgViewport) => {
    extentRef.current = [
      [viewport.minX, viewport.minY],
      [viewport.minX + viewport.w, viewport.minY + viewport.h],
    ];
  }, []);

  // The callback ref installs zoom when the SVG mounts after layout settles.
  // The explicit extent is in viewBox coordinates, preserving wheel-to-cursor
  // centering under a scaled viewBox. Native double-click zoom would conflict
  // with the node double-click action.
  const attachZoom = useCallback((svg: SVGSVGElement | null) => {
    svgRef.current = svg;
    if (!svg) {
      zoomRef.current = null;
      return;
    }
    const behavior = d3Zoom<SVGSVGElement, unknown>()
      .scaleExtent([ZOOM_MIN, Infinity])
      .extent(() => extentRef.current)
      .on("zoom", (event: D3ZoomEvent<SVGSVGElement, unknown>) => {
        setAnimateZoom(event.sourceEvent == null);
        setTransform(event.transform);
      });
    zoomRef.current = behavior;
    select(svg).call(behavior).on("dblclick.zoom", null);
  }, []);

  const applyTransform = useCallback((next: ZoomTransform) => {
    const svg = svgRef.current;
    const behavior = zoomRef.current;
    if (svg && behavior) behavior.transform(select(svg), next);
  }, []);

  const focusBounds = useCallback((
    bounds: SvgBounds,
    viewport: SvgViewport,
    padding: number,
    fitRatio: number,
  ) => {
    applyTransform(fitSvgBoxTransform(bounds, viewport, padding, fitRatio));
  }, [applyTransform]);

  const resetZoom = useCallback(() => applyTransform(zoomIdentity), [applyTransform]);

  const endZoomTransition = useCallback((propertyName: string) => {
    if (propertyName === "transform") setAnimateZoom(false);
  }, []);

  return { attachZoom, setExtent, transform, animateZoom, focusBounds, resetZoom, endZoomTransition };
}
