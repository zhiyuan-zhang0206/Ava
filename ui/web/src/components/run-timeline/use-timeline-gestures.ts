"use client";

// Pan/zoom gesture wiring for the run timeline (P4-1, task #4023): plain
// wheel pans (per animation frame), Ctrl/⌘+wheel zooms around the cursor
// (exp sensitivity 0.0022, the demo's), and a drag pans with grab semantics.
// Extracted from run-timeline-chart.tsx to keep that file under the source
// line budget — the behaviors and their review history are unchanged:
// gesture state lives in refs and finalization is unconditional, so a
// mid-gesture re-render can neither stall the drag nor leak the grab cursor
// or the click suppression (PR #2887 review, 3242).
//
// P4-2b (#4023): the same gestures drive the context axis' local viewport
// (`mode: "context"`) — char-domain pan/zoom over the fetched messages that
// never refetches, through the same refs discipline.

import { useEffect, useRef, type RefObject } from "react";

import { panContextView, zoomContextViewAround, type TimelineContextView } from "./context-view";
import { panWindow, zoomWindowAround, type TimelineWindowOverride } from "./request-level";

// Interaction detail, not a user setting: a drag shorter than this stays a
// click on the block under the pointer (the demo used the same threshold);
// only longer movements turn the gesture into a pan.
const DRAG_THRESHOLD_PX = 4;

export function useTimelineGestures({
  visualizationRef,
  plot,
  mode,
  window: timelineWindow,
  zoomWindow,
  contextView,
  contextTotal,
  onContextView,
  suppressClickRef,
  setDragging,
}: {
  visualizationRef: RefObject<HTMLDivElement | null>;
  plot: { left: number; width: number };
  /** P4-2b: which x domain the gestures drive — the fetch window (time) or
   *  the local char viewport (context; no refetch either way). */
  mode: "time" | "context";
  /** The committed window; gesture frames pan/zoom from the newest value. */
  window: TimelineWindowOverride;
  /** The page re-creates its closure every render, so it is read through a
   *  ref — a dependency on that identity would tear the effects down on
   *  every parent render mid-gesture (PR #2887 review, 3242). */
  zoomWindow: (window: TimelineWindowOverride) => void;
  /** P4-2b: the committed context viewport and its domain size. */
  contextView?: TimelineContextView;
  contextTotal?: number;
  /** P4-2b: context-viewport updates (read through a ref, like zoomWindow —
   *  the page re-creates the closure every render). */
  onContextView?: (view: TimelineContextView) => void;
  /** Cleared here and read by the chart's click suppression. */
  suppressClickRef: RefObject<boolean>;
  setDragging: (dragging: boolean) => void;
}): void {
  // Wheel/drag gestures batch into frames; the flush reads the latest
  // committed window so successive frames pan from the newest window (P4-1).
  const latestWindowRef = useRef(timelineWindow);
  const dragRef = useRef<{ startX: number; base: TimelineWindowOverride; moved: boolean } | null>(
    null,
  );
  const dragFrameRef = useRef(0);
  const dragPendingDxRef = useRef(0);
  const onZoomWindowRef = useRef(zoomWindow);
  const latestContextRef = useRef<{ view?: TimelineContextView; total?: number }>({
    view: contextView,
    total: contextTotal,
  });
  const dragContextBaseRef = useRef<{ view?: TimelineContextView; total?: number } | null>(null);
  const onContextViewRef = useRef(onContextView);

  useEffect(() => {
    onZoomWindowRef.current = zoomWindow;
  });

  useEffect(() => {
    onContextViewRef.current = onContextView;
  });

  useEffect(() => {
    latestWindowRef.current = timelineWindow;
  }, [timelineWindow]);

  useEffect(() => {
    latestContextRef.current = { view: contextView, total: contextTotal };
  }, [contextView, contextTotal]);

  useEffect(() => {
    const visualization = visualizationRef.current;
    if (!visualization) return;
    let frame = 0;
    let pendingPan = 0;
    const flushPan = () => {
      frame = 0;
      const fraction = pendingPan;
      pendingPan = 0;
      if (fraction === 0) return;
      if (mode === "context") {
        const { view, total } = latestContextRef.current;
        if (view === undefined || total === undefined) return;
        onContextViewRef.current?.(panContextView(view, fraction, total));
        return;
      }
      onZoomWindowRef.current(panWindow(latestWindowRef.current, fraction, new Date()));
    };
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const bounds = visualization.getBoundingClientRect();
      const cursorX = event.clientX - bounds.left;
      const anchor = Math.max(0, Math.min(1, (cursorX - plot.left) / plot.width));
      if (event.ctrlKey || event.metaKey) {
        if (event.deltaY === 0) return;
        const factor = Math.exp(event.deltaY * 0.0022);
        if (mode === "context") {
          const { view, total } = latestContextRef.current;
          if (view === undefined || total === undefined) return;
          onContextViewRef.current?.(zoomContextViewAround(view, factor, anchor, total));
          return;
        }
        onZoomWindowRef.current(
          zoomWindowAround(latestWindowRef.current, factor, anchor, new Date()),
        );
        return;
      }
      const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
      pendingPan += (delta / Math.max(200, plot.width)) * 1.15;
      if (frame === 0) frame = requestAnimationFrame(flushPan);
    };
    visualization.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      visualization.removeEventListener("wheel", onWheel);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [mode, plot.left, plot.width, visualizationRef]);

  useEffect(() => {
    const visualization = visualizationRef.current;
    if (!visualization) return;
    const flushPan = () => {
      dragFrameRef.current = 0;
      const drag = dragRef.current;
      const dx = dragPendingDxRef.current;
      dragPendingDxRef.current = 0;
      if (!drag || !drag.moved || dx === 0) return;
      if (mode === "context") {
        const base = dragContextBaseRef.current;
        if (base?.view === undefined || base.total === undefined) return;
        onContextViewRef.current?.(
          panContextView(base.view, -dx / Math.max(200, plot.width), base.total),
        );
        return;
      }
      onZoomWindowRef.current(
        panWindow(drag.base, -dx / Math.max(200, plot.width), new Date()),
      );
    };
    const onPointerDown = (event: PointerEvent) => {
      if (event.button !== 0) return;
      dragRef.current = { startX: event.clientX, base: latestWindowRef.current, moved: false };
      dragContextBaseRef.current = latestContextRef.current;
      dragPendingDxRef.current = 0;
      suppressClickRef.current = false;
    };
    const onPointerMove = (event: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const dx = event.clientX - drag.startX;
      if (!drag.moved) {
        if (Math.abs(dx) <= DRAG_THRESHOLD_PX) return;
        drag.moved = true;
        setDragging(true);
      }
      dragPendingDxRef.current = dx;
      if (dragFrameRef.current === 0) {
        dragFrameRef.current = requestAnimationFrame(flushPan);
      }
    };
    const endDrag = () => {
      const drag = dragRef.current;
      if (!drag) return;
      if (dragFrameRef.current !== 0) {
        cancelAnimationFrame(dragFrameRef.current);
        dragFrameRef.current = 0;
      }
      // Flush whatever motion is still pending, then clear the grab state.
      flushPan();
      dragRef.current = null;
      if (drag.moved) {
        suppressClickRef.current = true;
        // The click that follows pointerup lands within a frame; clear the
        // flag right after so it can never stick.
        window.setTimeout(() => {
          suppressClickRef.current = false;
        }, 150);
      }
      setDragging(false);
    };
    visualization.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", endDrag);
    window.addEventListener("pointercancel", endDrag);
    return () => {
      visualization.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", endDrag);
      window.removeEventListener("pointercancel", endDrag);
      if (dragFrameRef.current !== 0) {
        cancelAnimationFrame(dragFrameRef.current);
        dragFrameRef.current = 0;
      }
    };
  }, [mode, plot.width, setDragging, suppressClickRef, visualizationRef]);
}
