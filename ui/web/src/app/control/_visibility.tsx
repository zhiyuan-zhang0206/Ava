"use client";

// Section visibility — the primitive behind the Control page's "structure
// always in the DOM, data fetched from first paint" rule.
//
// The vertical Control page renders every section's scaffolding at once (so
// Ctrl+F and URL anchors always resolve) AND starts every section's data
// queries immediately on mount — the page must land fully populated, not
// waiting for scroll to reach a section (that read as a broken "collapsed
// until scrolled to" page). Firing everything on mount is still bounded: with
// this many sections' polls (Status 10s + Schedules 15s + Config's three
// parallel reads + Metrics, ...) running forever regardless of scroll would
// blow the per-connection budget once
// several tabs/users have the page open — so each section is wrapped in a
// visibility provider that keeps polling only while on (or near) screen, via
// an IntersectionObserver flipping a boolean as the section scrolls in/out of
// the viewport. Off-screen after the initial fetch ⇒ query disabled ⇒ no more
// polling; already-fetched data stays cached and rendered, so scrolling a
// loaded section out of view doesn't blank it — only its polling pauses.

import { createContext, type ReactNode, useCallback, useContext, useRef, useState } from "react";

// Default true: a section rendered outside a provider (a direct
// `/control/<sub>` route, or a unit test that doesn't scroll) fetches
// immediately, preserving the pre-vertical-page behavior.
const SectionVisibilityContext = createContext<boolean>(true);

/** Whether the enclosing Control section is on (or near) screen. Feed into a
 *  query's `enabled` so off-screen sections stop fetching/polling. */
export function useSectionVisible(): boolean {
  return useContext(SectionVisibilityContext);
}

// Margin added to the root's bounding box before computing intersections —
// a section keeps polling a little after it scrolls past the viewport edge,
// so the common small-scroll case never shows a loading flash right at the
// boundary. Irrelevant to the FIRST fetch (that always happens on mount, see
// below); this only affects when polling pauses/resumes after that.
const PREFETCH_PX = 300;
const ROOT_MARGIN = `${PREFETCH_PX}px 0px ${PREFETCH_PX}px 0px`;

/**
 * Observe an element's viewport intersection. Returns a ref callback to attach
 * and the live in-view boolean. Root is the viewport (`null`) — the Control
 * scroll container fills it, so viewport intersection tracks on-screen-ness
 * without threading the container ref down.
 *
 * Starts `true` unconditionally (not seeded from the element's on-mount
 * position) so every section fires its first fetch immediately regardless of
 * where it sits on the page — the whole page is "expanded" from first paint.
 * The IntersectionObserver then takes over for ongoing on-screen tracking,
 * pausing a section's polling once it has actually scrolled out of view.
 */
export function useInView(): [(node: Element | null) => void, boolean] {
  const [inView, setInView] = useState(true);
  const observerRef = useRef<IntersectionObserver | null>(null);

  const setRef = useCallback((node: Element | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (node === null) return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) setInView(entry.isIntersecting);
      },
      { rootMargin: ROOT_MARGIN },
    );
    observer.observe(node);
    observerRef.current = observer;
  }, []);

  return [setRef, inView];
}

/** Provide a fixed visibility to children (used by the section wrapper). */
export function SectionVisibilityProvider({
  visible,
  children,
}: {
  visible: boolean;
  children: ReactNode;
}) {
  return (
    <SectionVisibilityContext.Provider value={visible}>
      {children}
    </SectionVisibilityContext.Provider>
  );
}
