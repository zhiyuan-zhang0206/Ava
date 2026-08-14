"use client";

import { useEffect, useState } from "react";

/**
 * SSR-safe media query hook. Returns `false` during SSR (defaulting to
 * the smaller/mobile layout for safety), then syncs to the real match
 * after mount.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);

  useEffect(() => {
    const mql = window.matchMedia(query);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR-safe hydration: sync matchMedia after mount
    setMatches(mql.matches);
    const handler = (e: MediaQueryListEvent) => setMatches(e.matches);
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, [query]);

  return matches;
}
