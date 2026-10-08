"use client";

// The pointer gestures of the cluster view's shared time axis: the wheel / pinch zooms around the
// cursor, a horizontal scroll or a drag pans. Same feel and the same viewport math as the run
// timeline's rows (`../run-timeline/run-timeline-rows`); the expanded single-agent chart inside a
// lane handles its own gestures, so events that start in it are left to it.

import { useEffect, useRef, useState, type PointerEvent, type RefObject } from "react";

import { panViewport, zoomViewport, type Viewport } from "../run-timeline/timeline-model";

const WHEEL_ZOOM_RATE = 0.0015;
const PINCH_ZOOM_RATE = 0.01;
// A pointer must travel this far before a press becomes a pan (below it, it is a click).
const DRAG_THRESHOLD_PX = 4;
// Track width assumed until the first measurement.
const DEFAULT_TRACK_PX = 1000;
// Elements whose own gestures the shared axis leaves alone.
const OWN_GESTURES = '[data-testid="run-timeline-chart"], button, input, select, a, [data-no-pan]';

function firstTrack(root: HTMLElement | null): Element | null {
  return root?.querySelector("[data-track]") ?? null;
}

/**
 * The pixel width of the tracks inside the chart frame `ref`: the frame less its border (1px each side),
 * padding (12px each side) and the label column with its gap (88px) — the geometry of the run
 * timeline's rows, so the lanes, the curves and an opened lane's rows share one axis.
 */
export const TRACK_CHROME_PX = 2 + 24 + 88;

export function useTrackWidth(ref: RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(DEFAULT_TRACK_PX);
  useEffect(() => {
    const frame = ref.current;
    if (!frame) return;
    const measure = () => {
      const measured = frame.getBoundingClientRect().width - TRACK_CHROME_PX;
      if (measured > 0) setWidth(measured);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(frame);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

export interface GestureHandlers {
  onPointerDown: (event: PointerEvent<HTMLElement>) => void;
  onPointerMove: (event: PointerEvent<HTMLElement>) => void;
  onPointerUp: () => void;
  onPointerCancel: () => void;
  onClickCapture: (event: React.MouseEvent<HTMLElement>) => void;
}

/** Wheel zoom (native, non-passive, so the page does not scroll under it) and drag pan on `ref`'s chart. */
export function useViewGestures(
  ref: RefObject<HTMLElement | null>,
  view: Viewport,
  base: Viewport,
  onView: (view: Viewport) => void,
): GestureHandlers {
  const live = useRef({ view, base, onView });
  useEffect(() => {
    live.current = { view, base, onView };
  });
  const drag = useRef<{ x: number; view: Viewport; panning: boolean; id: number } | null>(null);
  const suppressClick = useRef(false);

  useEffect(() => {
    const chart = ref.current;
    if (!chart) return;
    const onWheel = (event: WheelEvent) => {
      if (event.target instanceof Element && event.target.closest(OWN_GESTURES)) return;
      const track = firstTrack(chart)?.getBoundingClientRect();
      if (!track || track.width <= 0) return;
      const { view: current, base: extent } = live.current;
      event.preventDefault();
      const horizontal = !event.ctrlKey && Math.abs(event.deltaX) > Math.abs(event.deltaY);
      const rate = event.ctrlKey ? PINCH_ZOOM_RATE : WHEEL_ZOOM_RATE;
      const next = horizontal
        ? panViewport(current, extent, event.deltaX / track.width)
        : zoomViewport(current, extent, (event.clientX - track.left) / track.width, Math.exp(event.deltaY * rate));
      // Events can arrive faster than React renders: the next one starts from this result.
      live.current = { ...live.current, view: next };
      live.current.onView(next);
    };
    chart.addEventListener("wheel", onWheel, { passive: false });
    return () => chart.removeEventListener("wheel", onWheel);
  }, [ref]);

  return {
    onPointerDown: (event) => {
      const track = firstTrack(ref.current)?.getBoundingClientRect();
      const own = event.target instanceof Element && event.target.closest(OWN_GESTURES);
      if (event.button !== 0 || !track || event.clientX < track.left || own) return;
      drag.current = { x: event.clientX, view: live.current.view, panning: false, id: event.pointerId };
    },
    onPointerMove: (event) => {
      const state = drag.current;
      const track = firstTrack(ref.current)?.getBoundingClientRect();
      if (!state || !track || track.width <= 0) return;
      const dx = event.clientX - state.x;
      if (!state.panning) {
        if (Math.abs(dx) < DRAG_THRESHOLD_PX) return;
        state.panning = true;
        // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- jsdom has no pointer capture
        event.currentTarget.setPointerCapture?.(state.id);
      }
      live.current.onView(panViewport(state.view, live.current.base, -dx / track.width));
    },
    onPointerUp: endDrag,
    onPointerCancel: endDrag,
    onClickCapture: (event) => {
      if (suppressClick.current) {
        event.stopPropagation();
        event.preventDefault();
      }
    },
  };

  function endDrag() {
    if (drag.current?.panning) {
      // The release of a pan would otherwise click the block under the pointer.
      suppressClick.current = true;
      setTimeout(() => {
        suppressClick.current = false;
      }, 0);
    }
    drag.current = null;
  }
}
