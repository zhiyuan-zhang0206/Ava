// SSE reduction and compact-envelope detection for the selected timeline.
// Store actions own notifications, buffer release, and epoch changes.

import { captureCompactTransition } from "./compact-transition";
import { foldEvent } from "./fold/timeline";
import { isEventForThread } from "./timeline";
import type { LiveCompact, TimelineState } from "./timeline-store";
import type { BackendTimelineItem, SystemEvent } from "./types";

/**
 * The timeline kinds that mark a history rewrite — the compact envelope rows
 * (`inbound_compact_request` = UI-triggered force compact,
 * `inbound_compact_summary` = agent-initiated `ava.self.compact`).
 */
const COMPACT_ENVELOPE_KINDS: ReadonlySet<string> = new Set([
  "inbound_compact_request",
  "inbound_compact_summary",
]);

/** Detect an unseen compact envelope by its stable rendered identity.
 * This is not a directional checkpoint revision; the ordering redesign must
 * replace it before arbitrary delayed same-agent reads can be proven safe. */
export function crossedUnseenCompact(
  local: BackendTimelineItem[],
  incoming: BackendTimelineItem[],
): boolean {
  return incoming.some((env) => {
    if (!COMPACT_ENVELOPE_KINDS.has(env.kind)) return false;
    const localEnv = local.find((it) => it.item_id === env.item_id);
    if (localEnv === undefined) return true;
    return localEnv.kind !== env.kind || localEnv.created_at !== env.created_at;
  });
}

/**
 * Retire the live compact whose summary item just landed in `items` — the
 * item is the durable view the ticking block hands over to. Returns the same
 * reference while nothing matches, so per-field selectors stay quiet.
 */
export function retireLiveCompact(
  live: LiveCompact | null,
  items: readonly BackendTimelineItem[],
): LiveCompact | null {
  if (live === null) return live;
  return items.some((it) => it.compact_id === live.compactId) ? null : live;
}

/**
 * The per-event reducer shared by `processSseEvent` and
 * `processSseEventBatch` — one event, one state, one partial to merge
 * ({} = nothing changed). Pure: both entry points route through it, so the
 * batch path can never diverge from the per-event path.
 */
export function applySseEvent(state: TimelineState, ev: SystemEvent): Partial<TimelineState> {
  // Only selected-agent token events write the context bar. Selection resets
  // these fields; useTokenUsage supplies the newly selected HTTP snapshot.
  // agent_id=0 system resets follow the shared event-routing rule.
  if (ev.role === "token_usage") {
    return isEventForThread(ev, state.activeThreadId)
      ? { tokenUsage: ev.input_tokens, reasoningTokens: ev.reasoning_tokens ?? 0 }
      : {};
  }

  // ACTIVE thread (or agent_id=0 system signal): fold into the top-level
  // fields. This is the rendered thread, so its updates drive the UI.
  if (isEventForThread(ev, state.activeThreadId)) {
    if (ev.role === "compact_started") {
      // A new run supersedes any previous entry; the stale run's terminal,
      // if it still arrives, is dropped by the compact_id pairing below.
      return {
        liveCompact: {
          compactId: ev.compact_id,
          startedAt: ev.started_at,
          mode: ev.mode,
          status: null,
          finishedAt: null,
        },
      };
    }
    if (ev.role === "compact_finished") {
      // Pair by compact_id — a terminal for a superseded run is stale and
      // dropped. An entry-less terminal (start lost in an SSE gap) still
      // records the outcome so the block can show it briefly.
      if (state.liveCompact !== null && state.liveCompact.compactId !== ev.compact_id) {
        return {};
      }
      return {
        liveCompact: {
          compactId: ev.compact_id,
          startedAt: state.liveCompact?.startedAt ?? null,
          mode: state.liveCompact?.mode ?? null,
          status: ev.status,
          finishedAt: ev.finished_at,
        },
      };
    }
    // Keep the visible content until the compact replacement arrives.
    if (ev.role === "compact_done") {
      return {
        streamingIds: new Set(),
        // Old foldEvent treated compact_done as a code-end (streamingCode
        // false, turnActive untouched); preserve that flag behavior.
        streamingCode: false,
        loadingOlder: false,
        olderFetchCount: 0,
        resetPending: true,
      };
    }
    // The first nonempty compact snapshot replaces the selected segment.
    if (ev.role === "timeline_snapshot") {
      const snapItems = ev.items as unknown as BackendTimelineItem[];
      // A full-window snapshot (0.0 present) whose compact envelope the local
      // items never folded is the post-compact history a missed compact_done
      // should have armed the window for (SSE gap) — replace wholesale so the
      // compacted-away items cannot resurrect, same as the reset window.
      const crossedCompact =
        snapItems.some((it) => it.item_id === "0.0") &&
        crossedUnseenCompact(state.items, snapItems);
      if (state.resetPending || crossedCompact) {
        if (snapItems.length === 0) return {};
        const epoch = state.compactEpoch + 1;
        return {
          items: snapItems,
          compactEpoch: epoch,
          compactBuffer: state.compactHistoryPages === 0
            ? null
            : captureCompactTransition(state.items, state.compactBuffer, ev.agent_id, epoch, false),
          streamingIds: new Set(),
          resetPending: false,
          compactReplaceSeq: state.compactReplaceSeq + 1,
          compactReplaceAgent: ev.agent_id,
          hasMoreOlder: state.hasMoreOlder,
          liveCompact: retireLiveCompact(state.liveCompact, snapItems),
        };
      }
    }
    const next = foldEvent(
      {
        items: state.items,
        streamingIds: state.streamingIds,
        streamingCode: state.streamingCode,
        turnActive: state.turnActive,
        hasMoreOlder: state.hasMoreOlder,
        olderFetchCount: state.olderFetchCount,
        // The selected thread owns the compact reset window.
        resetPending: state.resetPending,
      },
      ev,
    );
    // Unchanged fields keep their references (foldEvent carries them via
    // ...t / no-op reducers), so per-field Zustand selectors short-circuit.
    return {
      items: next.items,
      streamingIds: next.streamingIds,
      streamingCode: next.streamingCode,
      turnActive: next.turnActive,
      hasMoreOlder: next.hasMoreOlder,
      liveCompact: retireLiveCompact(state.liveCompact, next.items),
    };
  }

  return {};
}
