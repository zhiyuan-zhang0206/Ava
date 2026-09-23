"use client";

import { useSyncExternalStore } from "react";

/** Nothing ever changes — the "store" is the client's own existence. */
const never = () => () => {
  // No subscription to release.
};

/**
 * False through SSR and the hydrating render, then true; TRUE already on the
 * first render of a client-side mount. The sanctioned `useSyncExternalStore`
 * server-snapshot pattern for "is this frame shaped by the server snapshot or
 * by the client?".
 *
 * The home layout uses it as its placeholder gate: a panel group must not
 * register before the breakpoint values are the client's own (registering
 * the SSR-safe mobile frame on a hydrating desktop load would remount the
 * group — the note on `HomeLayout`). A `useEffect`-flipped `mounted` flag
 * answered "not yet" on client-side navigations too, so every back/forward
 * into the home page painted one blank placeholder frame; keying the gate on
 * the snapshot's origin removes that frame without touching the hydration
 * contract.
 */
export function useClientReady(): boolean {
  return useSyncExternalStore(never, () => true, () => false);
}
