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

import { createContext, useContext, useEffect, useState } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";

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
 *  execution transition) or on turn end; the committed-duration guard keeps a
 *  block whose clearing event was missed from ticking once its
 *  code_elapsed_ms lands, mirroring the reasoning / execution predicates. */
export function isLiveCode(item: BackendTimelineItem): boolean {
  return (
    item.kind === "agent_code" &&
    item.codeStartedAt != null &&
    item.code_elapsed_ms == null &&
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

const LIVE_CLOCK_INTERVAL_MS = 100;

// One shared 100ms ticker for every live timeline clock (block chips, the
// turn header, the compacting block). Each tick hands the same timestamp to
// every subscriber inside one timer task, so React batches all live clocks
// into a single render pass instead of one per independently phased
// interval. The interval exists only while at least one clock is live.
const listeners = new Set<(now: number) => void>();
let tickerId: ReturnType<typeof setInterval> | null = null;

function subscribeTick(listener: (now: number) => void): () => void {
  listeners.add(listener);
  tickerId ??= setInterval(() => {
    const now = Date.now();
    for (const notify of listeners) notify(now);
  }, LIVE_CLOCK_INTERVAL_MS);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && tickerId !== null) {
      clearInterval(tickerId);
      tickerId = null;
    }
  };
}

/** Number of live clocks currently subscribed to the shared ticker. */
export function liveClockSubscriberCount(): number {
  return listeners.size;
}

// Whether the agent is busy. TimelineView provides it; a clock outside a busy
// agent never ticks, so a block whose live stamp outlived a missed turn-end
// event freezes instead of re-rendering 10x/s on an idle page. The default
// (no provider) leaves gating to the caller's own `active` flag.
export const LiveClockGate = createContext(true);

// Re-render on the shared ticker while `active` (and the agent is busy), so a
// "Thinking for Xs" clock ticks; idle (committed / non-reasoning) callers pass
// false and never subscribe.
export function useNow(active: boolean): number {
  const gate = useContext(LiveClockGate);
  const ticking = active && gate;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return;
    return subscribeTick(setNow);
  }, [ticking]);
  return now;
}
