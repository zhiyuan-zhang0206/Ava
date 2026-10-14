"use client";

import { type RefObject, useCallback, useLayoutEffect, useRef } from "react";

// Separate enter/exit speeds and a settling interval prevent threshold flicker.
const FAST_PX_PER_MS = 1.8;
const SLOW_PX_PER_MS = 0.6;
const SLOW_SETTLE_MS = 120;
const IDLE_SETTLE_MS = 160;

interface ScrollMotion {
  top: number | null;
  at: number | null;
  suppressed: boolean;
  slowSince: number | null;
}

/** Temporarily let pinned titles scroll in flow during rapid movement. */
export function useStickyHeaderMotion(
  wrapperRef: RefObject<HTMLDivElement | null>,
  threadKey: string | undefined,
  onRestore: () => void,
): (viewport: HTMLElement, compensated: boolean) => void {
  const motionRef = useRef<ScrollMotion>({ top: null, at: null, suppressed: false, slowSince: null });
  const idleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const restore = useCallback(() => {
    if (idleTimerRef.current !== null) clearTimeout(idleTimerRef.current);
    idleTimerRef.current = null;
    motionRef.current.suppressed = false;
    motionRef.current.slowSince = null;
    delete wrapperRef.current?.dataset.fastScrolling;
    onRestore();
  }, [wrapperRef, onRestore]);

  useLayoutEffect(() => {
    const wrapper = wrapperRef.current;
    const viewport = wrapper?.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]');
    motionRef.current = { top: viewport?.scrollTop ?? null, at: null, suppressed: false, slowSince: null };
    if (wrapper) delete wrapper.dataset.fastScrolling;
    return () => {
      if (idleTimerRef.current !== null) clearTimeout(idleTimerRef.current);
      idleTimerRef.current = null;
      if (wrapper) delete wrapper.dataset.fastScrolling;
    };
  }, [wrapperRef, threadKey]);

  return useCallback((viewport: HTMLElement, compensated: boolean) => {
    const motion = motionRef.current;
    const now = performance.now();
    const distance = Math.abs(viewport.scrollTop - (motion.top ?? viewport.scrollTop));
    const elapsed = motion.at === null ? 16 : now - motion.at;
    motion.top = viewport.scrollTop;
    motion.at = now;
    if (compensated || distance === 0) return;

    // The first movement after a pause belongs to a frame, not the whole idle gap.
    const speed = distance / (elapsed > IDLE_SETTLE_MS ? 16 : Math.max(8, elapsed));
    if (!motion.suppressed && speed >= FAST_PX_PER_MS) {
      motion.suppressed = true;
      if (wrapperRef.current) wrapperRef.current.dataset.fastScrolling = "true";
    }
    if (!motion.suppressed) return;

    motion.slowSince = speed <= SLOW_PX_PER_MS ? motion.slowSince ?? now : null;
    if (motion.slowSince !== null && now - motion.slowSince >= SLOW_SETTLE_MS) {
      restore();
      return;
    }
    if (idleTimerRef.current !== null) clearTimeout(idleTimerRef.current);
    idleTimerRef.current = setTimeout(restore, IDLE_SETTLE_MS);
  }, [wrapperRef, restore]);
}
