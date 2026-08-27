// Timeline reducer entry point — re-exports the pure fold domain
// (lib/fold/timeline.ts, extracted 2026-08-08, R4 layer 1) so the historical
// importers (tests, useTimeline, use-pending-messages) resolve unchanged.
//
// isEventForThread lives here: it is a hook-level SSE gate, not fold logic.

import type { BackendTimelineItem } from "./types";

export {
  applySystemEvent,
  clearCodeClocks,
  freezeReasoningClocks,
  mergeSnapshotWithStreaming,
  parseItemId,
  parseItemIdParts,
  sortByItemId,
} from "./fold/timeline";

/** Standing context the gateway re-attaches ahead of the initial timeline window. */
export function isReattachedTimelineContext(item: BackendTimelineItem): boolean {
  return item.kind === "system_prompt" || item.kind === "inbound_compact_summary";
}

/** Item ids of the standing head notes: the contiguous `system_marker` run
 *  right after the current segment's system prompt (`0.0`).
 *
 *  These are the notes `agent/graph/_context_notes.py` lays down at window
 *  establishment (exec timeout / timezone / cluster memory / agent id / agent
 *  memory / preloaded skills). The gateway re-attaches them at the head of
 *  every initial window exactly like the prompt itself, so loadOlder cursors
 *  and scroll anchors must skip them the same way they skip the prompt —
 *  otherwise the first scroll-up would use a head note as the `before=`
 *  cursor and the gateway would cross straight to the older compact segment,
 *  skipping every real item between the head and the tail window.
 *
 *  Only the CURRENT segment's run is returned: historical segments carry
 *  segment-prefixed ids and are paged by their own standing-context rules. */
export function standingHeadNoteIds(items: readonly BackendTimelineItem[]): Set<string> {
  const ids = new Set<string>();
  const promptIdx = items.findIndex((it) => it.item_id === "0.0");
  if (promptIdx < 0) return ids;
  let i = promptIdx + 1;
  while (i < items.length && items[i].kind === "system_marker") {
    ids.add(items[i].item_id);
    i += 1;
  }
  return ids;
}

export function isEventForThread(
  ev: { readonly agent_id: number },
  activeThreadId: number | null,
): boolean {
  // agent_id=0 is a system-level signal (token reset etc.); does not belong to any thread — let it through
  if (ev.agent_id === 0) return true;
  // When activeThreadId=null (uninitialized before first mount),
  // ev.agent_id (number) === null is naturally false — no separate
  // early return needed.
  return ev.agent_id === activeThreadId;
}
