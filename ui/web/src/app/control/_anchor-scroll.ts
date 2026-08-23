"use client";

// Deep-link anchor scrolling for the two vertical anchor-nav pages (Control
// and Insights). A mount-time hash scroll is not enough on its own: section
// bodies fetch asynchronously (live status data and section lists), so content
// ABOVE the target grows after the first
// scrollIntoView and pushes the anchored section below where it landed — on
// /insights#alerts the section ended up at the bottom of the viewport (or
// below the fold entirely, when an async section above it settled last). This hook
// re-applies the scroll while the scroll container's content height is still
// settling, then detaches — the user's own scrolling is never fought.

import { useEffect } from "react";

// Re-scroll only while the content height keeps changing; stop once it has
// been stable this long (the async sections above the target have landed).
const SETTLE_STABLE_MS = 500;
// Hard cap — never keep re-scrolling for longer than this after mount.
const SETTLE_CAP_MS = 5000;
// A genuine user scroll during the settle window moves the container by far
// more than this; a content-height change without user input leaves it (or
// clamps it by a couple of px). Past this slack we stop fighting.
const USER_SCROLL_SLACK_PX = 32;

/**
 * Scroll `targetId` to the top of the `scrollId` container on mount, and
 * re-apply the scroll whenever the container's content height changes until
 * it settles (or the cap elapses). A no-op when `targetId` is null or the
 * target element is missing — the caller resolves the URL hash.
 */
export function useSettledAnchorScroll(scrollId: string, targetId: string | null): void {
  useEffect(() => {
    if (!targetId) return;
    const container = document.getElementById(scrollId);
    const scroll = () => document.getElementById(targetId)?.scrollIntoView({ block: "start" });
    scroll();
    // No ResizeObserver (happy-dom tests): the initial scroll is the whole
    // behavior — also the correct result when the content is already settled.
    if (!container || typeof ResizeObserver === "undefined") return;

    let lastHeight = container.scrollHeight;
    let lastScrollTop = container.scrollTop;
    let stopped = false;
    let stableTimer: number | undefined;

    const observer = new ResizeObserver(() => {
      if (stopped) return;
      const height = container.scrollHeight;
      if (height === lastHeight) return;
      lastHeight = height;
      // The user took over scrolling during the settle window — stop.
      if (Math.abs(container.scrollTop - lastScrollTop) > USER_SCROLL_SLACK_PX) {
        observer.disconnect();
        stopped = true;
        return;
      }
      scroll();
      lastScrollTop = container.scrollTop;
      window.clearTimeout(stableTimer);
      stableTimer = window.setTimeout(() => {
        observer.disconnect();
        stopped = true;
      }, SETTLE_STABLE_MS);
    });
    // Watch the content wrapper: the container's own box does not change as
    // its content grows — only the wrapper's height does.
    const content = container.firstElementChild;
    if (content) observer.observe(content);
    const cap = window.setTimeout(() => {
      observer.disconnect();
      stopped = true;
    }, SETTLE_CAP_MS);
    return () => {
      observer.disconnect();
      window.clearTimeout(stableTimer);
      window.clearTimeout(cap);
    };
  }, [scrollId, targetId]);
}
