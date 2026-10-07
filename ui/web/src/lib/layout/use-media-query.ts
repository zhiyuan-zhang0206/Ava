"use client";

import { useCallback, useSyncExternalStore } from "react";

/** Stable server snapshot — queried during SSR and the hydrating render. */
const serverSnapshot = () => false;

/**
 * SSR-safe media query hook. The match is read through
 * `useSyncExternalStore`: on the client the REAL match is available in the
 * first render (a client-side navigation renders the real frame in its first
 * commit — no post-mount flip), while SSR and hydration use the server
 * snapshot (`false`, the mobile-first frame) and re-render once the client
 * store answers. A desktop load therefore still paints the mobile frame
 * until hydration completes — the accepted contract, condensed in
 * `useBreakpoint`'s note.
 *
 * Previously the value lived in `useState` + `useEffect`: every CLIENT-side
 * mount committed the mobile-first frame first and corrected it after the
 * passive flush, which is a painted wrong frame whenever a commit and the
 * flush straddle a rendering pass.
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      const mql = window.matchMedia(query);
      mql.addEventListener("change", onStoreChange);
      return () => mql.removeEventListener("change", onStoreChange);
    },
    [query],
  );
  const getSnapshot = useCallback(() => window.matchMedia(query).matches, [query]);
  return useSyncExternalStore(subscribe, getSnapshot, serverSnapshot);
}
