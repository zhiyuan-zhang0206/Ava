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
  sortByItemId,
} from "./fold/timeline";

/** Standing context the gateway re-attaches ahead of the initial timeline window. */
export function isReattachedTimelineContext(item: BackendTimelineItem): boolean {
  return item.kind === "system_prompt" || item.kind === "inbound_compact_summary";
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
