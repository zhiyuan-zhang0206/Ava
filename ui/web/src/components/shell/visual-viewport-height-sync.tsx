"use client";

import { useEffect } from "react";

/**
 * Visual-viewport shortfall (px) that reads as an on-screen keyboard rather
 * than browser chrome: iOS Safari's toolbar collapse/expand moves
 * visualViewport.height by tens of pixels, while even the smallest phone
 * keyboards are ~200px. Below the threshold the shell keeps its
 * layout-viewport height, so toolbar motion alone never shrinks the page.
 */
export const KEYBOARD_MIN_VISUAL_VIEWPORT_DELTA_PX = 150;

/**
 * Keeps the app shell in step with the visual viewport while the on-screen
 * keyboard is up (task #4779).
 *
 * The shell takes its height from the LAYOUT viewport (`h-full` on html/body
 * = 100% of the initial containing block) and the composer sits in normal
 * flow at the bottom of that column. A phone keyboard shrinks only the VISUAL
 * viewport, so the composer keeps its place at the layout bottom — under the
 * keyboard — and Safari pans the page to reveal the focused field (the
 * composer sliding around / the header drifting off-screen in the report).
 * Pinning <html> to visualViewport.height while the keyboard is up lets the
 * existing flex chain shrink by itself: the composer lands inside the visible
 * band and Safari has nothing to pan for.
 *
 * Android Chrome never needs this effect: the viewport export in
 * app/layout.tsx declares `interactive-widget=resizes-content`, so its
 * keyboard shrinks the layout viewport natively and the delta read here stays
 * ~0. While the user pinch-zooms (`scale !== 1`) the shell is left alone —
 * a keyboard pin would fight a viewport the user is already scaling.
 *
 * Renders nothing; mounted from app/layout.tsx — the same file that declares
 * the Android meta — so every route, login included, is covered.
 */
export function VisualViewportHeightSync() {
  useEffect(() => {
    const viewport = window.visualViewport;
    if (!viewport) return;

    const sync = () => {
      const keyboardUp =
        viewport.scale === 1 &&
        window.innerHeight - viewport.height > KEYBOARD_MIN_VISUAL_VIEWPORT_DELTA_PX;
      // Rounded: fractional visual-viewport heights would leak half pixels
      // into the shell height.
      document.documentElement.style.height = keyboardUp ? `${Math.round(viewport.height)}px` : "";
    };

    sync();
    // `resize` carries height changes (keyboard opens/closes, toolbar moves);
    // `scroll` catches Safari's pan-the-focused-field pass, which can land
    // after the resize.
    viewport.addEventListener("resize", sync);
    viewport.addEventListener("scroll", sync);
    return () => {
      viewport.removeEventListener("resize", sync);
      viewport.removeEventListener("scroll", sync);
      // Unmount must not leave the shell stuck at a keyboard height.
      document.documentElement.style.height = "";
    };
  }, []);

  return null;
}
