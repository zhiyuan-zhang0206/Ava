"use client";

import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from "react";

const PULL_SPACE_PX = 80;
const PULL_THRESHOLD_PX = 64;
const AT_TOP_PX = 1;

type Phase = "idle" | "scrolling" | "pulling" | "loading" | "returning";
interface PullGesture {
  phase: Phase;
  viewport: HTMLElement | null;
  momentum: boolean;
  blockMomentum: boolean;
  touchY: number | null;
  minHeight: string;
  offset: number;
}
const freshGesture = (): PullGesture => ({
  phase: "idle", viewport: null, momentum: false, blockMomentum: false, touchY: null, minHeight: "", offset: 0,
});

/** Native scroll space keeps finger pauses distinct from the end of a wheel gesture. */
export function useOlderHistoryPull(
  threadKey: string | undefined,
  spaceRef: RefObject<HTMLDivElement | null>,
  contentRef: RefObject<HTMLDivElement | null>,
  canLoad: () => boolean,
  loadPage: () => boolean,
  onUpwardIntent: () => void,
) {
  const latest = useRef({ canLoad, loadPage, onUpwardIntent });
  const gesture = useRef(freshGesture());
  const [visual, setVisual] = useState({ threadKey, offset: 0, returning: false });
  if (visual.threadKey !== threadKey) setVisual({ threadKey, offset: 0, returning: false });
  const contentHeight = useCallback((viewport: HTMLElement) =>
    viewport.scrollHeight - (gesture.current.viewport ? PULL_SPACE_PX : 0), []);

  const show = useCallback((offset: number, returning = false) => {
    setVisual((previous) => previous.offset === offset && previous.returning === returning
      ? previous : { ...previous, offset, returning });
  }, []);

  const clearSpace = useCallback(() => {
    const space = spaceRef.current;
    const current = gesture.current;
    const top = current.viewport?.scrollTop ?? 0;
    if (space) { space.style.transition = ""; space.style.height = "0px"; }
    if (contentRef.current) contentRef.current.style.minHeight = current.minHeight;
    if (current.viewport) current.viewport.scrollTop = Math.max(0, top - PULL_SPACE_PX);
  }, [spaceRef, contentRef]);

  const prepare = useCallback((viewport: HTMLElement) => {
    const space = spaceRef.current;
    const content = contentRef.current;
    if (!space || !content) return;
    const current = gesture.current;
    // Empty/exhausted views need no artificial scroll range. Once prepared,
    // keep it fixed through a request and its final history landing.
    if (!current.viewport && !latest.current.canLoad()) return;
    const creating = !current.viewport;
    if (creating) current.minHeight = content.style.minHeight;
    current.viewport = viewport;
    // Prepare before hit-testing the first wheel. Adding range from inside
    // that wheel loses its default scroll: Chrome already chose its target.
    content.style.minHeight = `${viewport.clientHeight}px`;
    space.style.height = `${PULL_SPACE_PX}px`;
    // Late history availability must not move a parked reader or Compact anchor.
    if (creating) viewport.scrollTop += PULL_SPACE_PX;
    if (current.phase === "idle" && viewport.scrollTop < PULL_SPACE_PX) viewport.scrollTop = PULL_SPACE_PX;
  }, [spaceRef, contentRef]);

  useLayoutEffect(() => {
    gesture.current = freshGesture();
    return clearSpace;
  }, [threadKey, clearSpace]);
  useLayoutEffect(() => {
    latest.current = { canLoad, loadPage, onUpwardIntent };
    const viewport = spaceRef.current?.closest<HTMLElement>('[data-slot="scroll-area-viewport"]');
    if (viewport) prepare(viewport);
  });

  const returnSpace = useCallback(() => {
    const current = gesture.current;
    const viewport = current.viewport;
    if (!viewport) return;
    current.phase = "returning";
    show(Math.max(0, PULL_SPACE_PX - viewport.scrollTop), true);
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    viewport.scrollTo({ top: PULL_SPACE_PX, behavior: reduced ? "instant" : "smooth" });
  }, [show]);

  const release = useCallback((commit: boolean) => {
    const current = gesture.current;
    if (current.phase !== "pulling" || !current.viewport) return;
    const offset = Math.max(0, PULL_SPACE_PX - current.viewport.scrollTop);
    current.blockMomentum = true;
    if (commit && offset >= PULL_THRESHOLD_PX && latest.current.canLoad()) {
      current.phase = "loading";
      show(offset);
      // Keep the reserve unchanged through capture, request and prepend.
      if (latest.current.loadPage()) return;
    }
    returnSpace();
  }, [returnSpace, show]);

  // Called AFTER the timeline's prepend compensation, in the same layout pass.
  const finishLoad = useCallback(() => {
    const current = gesture.current;
    const viewport = current.viewport;
    if (current.phase !== "loading" || !viewport) return;
    if (viewport.scrollTop < PULL_SPACE_PX) { returnSpace(); return; }
    // The reserve remains above the reading window. Keeping its height fixed
    // avoids any second reflow after the prepend anchor has been compensated.
    current.phase = "idle";
    show(0, true);
  }, [returnSpace, show]);

  const onScroll = useCallback((viewport: HTMLElement) => {
    const current = gesture.current;
    if (!current.viewport) return false;
    if (current.phase === "pulling" || current.phase === "returning") {
      const offset = Math.max(0, PULL_SPACE_PX - viewport.scrollTop);
      const hadOffset = current.offset > 0;
      current.offset = offset;
      show(offset, current.phase === "returning");
      if (offset === 0 && hadOffset && current.phase === "pulling") current.phase = "scrolling";
    } else if (current.phase !== "loading" && viewport.scrollTop < PULL_SPACE_PX) {
      // Arriving inertia/scrollbars stop at the reading boundary, before paint.
      viewport.scrollTop = PULL_SPACE_PX;
      return true;
    }
    return false;
  }, [show]);

  const onScrollEnd = useCallback(() => {
    const current = gesture.current;
    if (current.touchY !== null) return;
    if (current.phase === "pulling") release(true);
    else if (current.phase === "scrolling" || current.phase === "returning") {
      current.phase = "idle";
      show(0);
    }
  }, [release, show]);

  const onWheel = useCallback((event: WheelEvent, viewport: HTMLElement) => {
    const current = gesture.current;
    if (event.ctrlKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
      release(false);
      return;
    }
    if (event.deltaY === 0) return;
    if (event.deltaY < 0) latest.current.onUpwardIntent();
    if (!current.viewport) return;
    const momentum = (event as WheelEvent & { readonly momentum?: boolean }).momentum;
    const atTop = viewport.scrollTop <= PULL_SPACE_PX + AT_TOP_PX;
    if (momentum === true) {
      release(true);
      current.momentum = true;
      if (current.blockMomentum || (atTop && event.deltaY < 0)) event.preventDefault();
      return;
    }
    if (current.phase === "loading" || current.phase === "returning") {
      event.preventDefault();
      return;
    }
    if (momentum === false && current.momentum && current.phase !== "pulling") current.phase = "idle";
    current.momentum = false;
    if (current.phase === "idle") {
      current.blockMomentum = false;
      // Allow default scrolling in the prepared reserve so Chrome retains
      // the native gesture phase through stationary fingers and a slow lift.
      if (atTop && event.deltaY < 0 && latest.current.canLoad() && "onscrollend" in viewport) {
        current.phase = "pulling";
        current.offset = 0;
      }
      else if (!atTop) current.phase = "scrolling";
    }
    if (current.phase !== "pulling" && atTop && event.deltaY < 0) event.preventDefault();
  }, [release]);

  const onTouchStart = useCallback((event: TouchEvent, viewport: HTMLElement) => {
    const current = gesture.current;
    if (current.phase === "loading" || current.phase === "returning") return;
    current.touchY = event.touches.length === 1 ? event.touches[0].clientY : null;
    current.blockMomentum = false;
    if (current.touchY !== null && viewport.scrollTop <= PULL_SPACE_PX + AT_TOP_PX && latest.current.canLoad()) {
      current.phase = "pulling";
      current.offset = 0;
    } else current.phase = "scrolling";
  }, []);
  const onTouchMove = useCallback((event: TouchEvent, viewport: HTMLElement) => {
    const current = gesture.current;
    if (event.touches.length !== 1 || current.touchY === null) {
      current.touchY = null;
      release(false);
      return;
    }
    const nextY = event.touches[0].clientY;
    const distance = nextY - current.touchY;
    current.touchY = nextY;
    if (distance > 0) latest.current.onUpwardIntent();
    if (current.phase !== "pulling") return;
    event.preventDefault();
    viewport.scrollTop = Math.max(0, Math.min(PULL_SPACE_PX, viewport.scrollTop - distance));
    onScroll(viewport);
  }, [onScroll, release]);
  const onTouchEnd = useCallback(() => {
    gesture.current.touchY = null;
    release(true);
    if (gesture.current.phase === "scrolling") gesture.current.phase = "idle";
  }, [release]);
  const onTouchCancel = useCallback(() => {
    gesture.current.touchY = null;
    release(false);
  }, [release]);
  const isActive = useCallback(() => ["pulling", "loading", "returning"].includes(gesture.current.phase), []);

  const offset = visual.threadKey === threadKey ? visual.offset : 0;
  return { prepare, contentHeight, offset, progress: Math.min(1, offset / PULL_THRESHOLD_PX), returning: visual.returning,
    isActive, finishLoad, onWheel, onScroll, onScrollEnd, onTouchStart, onTouchMove, onTouchEnd, onTouchCancel };
}
