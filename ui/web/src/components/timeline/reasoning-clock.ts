// Live "Thinking for Xs" clock behind the thinking toggle chip. A thinking
// item is still streaming (this returns true) when it carries a frontend start
// stamp (reasoningStartedAt — set only by the SSE stream handlers, never
// present on snapshot-committed items) and no committed backend duration yet.
// The clock stops the moment a later block starts or the turn ends:
// freezeReasoningClocks drops reasoningStartedAt and stamps the block's elapsed
// into reasoningElapsedMs, so the chip shows a frozen "Thought for Xs" until
// the snapshot commits the authoritative reasoning_ms (which replaces the whole
// item, dropping both frontend stamps). Each reasoning block times itself —
// model-agnostic, no assumption that thinking precedes all text.
//
// Deliberately NOT gated on `partial`: that flag marks only the
// delta-before-start bootstrap and interrupted paths, so a normally streamed
// item never has it — requiring it would keep the clock from ever ticking on
// the common path.

import { useEffect, useState } from "react";

import type { BackendTimelineItem } from "@/lib/types";

export function isLiveReasoning(item: BackendTimelineItem): boolean {
  return (
    item.kind === "agent_reasoning" &&
    item.reasoningStartedAt != null &&
    item.reasoning_ms == null &&
    // An interrupted stream never gets its reasoning_ms commit — without
    // this the clock would tick forever on the dead item.
    !item.interrupted
  );
}

/** True while an agent_code item is still being streamed — the frontend
 *  stamp `codeStartedAt` is set by code_start, and no committed duration
 *  exists yet. The code toggle chip uses this to show a live "writing code
 *  for Xs" clock. The stamp is cleared when exec_start fires (code →
 *  execution transition) or on turn end. */
export function isLiveCode(item: BackendTimelineItem): boolean {
  return (
    item.kind === "agent_code" &&
    item.codeStartedAt != null &&
    !item.interrupted
  );
}

export function isLiveExecution(item: BackendTimelineItem): boolean {
  return (
    item.kind === "code_output" &&
    item.execStartedAt != null &&
    item.exec_ms == null &&
    // An interrupted stream never gets its exec_ms commit — without
    // this the clock would tick forever on the dead item.
    !item.interrupted
  );
}

// Re-render 10x/s (100ms interval) while `active`, so a "Thinking for Xs"
// clock ticks; idle
// (committed / non-reasoning) callers pass false and never start a timer.
export function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(id);
  }, [active]);
  return now;
}
