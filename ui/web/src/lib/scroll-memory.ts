// Per-history-entry scroll memory for the app's inner scroll containers.
//
// The document never scrolls here (html/body are h-full; every page scrolls
// inside its own element), so the browser's native history scroll restoration
// has nothing to restore: a back/forward remounts the page with its container
// reset -- the timeline re-pinned to the newest message, a terminal to its
// tail. Each surface saves its position under the history entry it belongs
// to, and a remount at the same entry restores it before the first paint.
//
// The key is Next's `router.bfcacheId`: stable across back/forward and
// `router.refresh()`, fresh on push/replace -- the client router restores it
// from its BFCache entry on a traverse regardless of `cacheComponents`
// (next/dist client router source). So a restore happens exactly when the
// browser returns to an entry it kept, never on a fresh navigation: a first
// visit keeps its own default (timeline pinned to the newest message,
// terminal pinned to the tail).
//
// In-memory by design: positions are per-tab view state, like the panels'
// splits; nothing is persisted, and the map is bounded by the tab's session
// history (one small record per entry).

export interface SavedScroll {
  /** Identity of the content the position belongs to (the agent for the home
   *  timeline, agent+session for a terminal pane). A mismatch on restore
   *  means the entry now shows other content -- the position is dropped. */
  contentKey: string;
  /** Offset of the scrolling element. */
  scrollTop: number;
  /** Whether the container was following the bottom (new output keeps
   *  pulling it down) when saved. A follower comes back following, not
   *  frozen at a stale offset. */
  followBottom: boolean;
}

const memory = new Map<string, SavedScroll>();

/** The position saved for this history entry, or null when none is. */
export function readScrollMemory(entryKey: string): SavedScroll | null {
  return memory.get(entryKey) ?? null;
}

/** Remember this history entry's position; a later save overwrites. */
export function saveScrollMemory(entryKey: string, value: SavedScroll): void {
  memory.set(entryKey, value);
}
