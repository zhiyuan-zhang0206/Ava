// Zustand store — SSE-driven streaming timeline state only.
//
// Split out of the app store (store.ts, which now holds pure-client UI +
// cluster-coordination state) so that the high-frequency SSE fold — one set()
// per code_delta / chat_delta chunk — notifies ONLY timeline subscribers.
// Zustand notifies every subscriber of a store on each set() and re-runs their
// selector; keeping the timeline in its own store means a burst of streaming
// deltas never runs the sidebar / spawn-dialog / cluster-banner selectors. The
// two stores never cross-read (the timeline gate `activeThreadId` and the
// sidebar selection `activeId` are coordinated only at the hook level, in
// useTimeline), so the split is a clean cut, not a shared-slice carve-out.
//
// Server data (agents list, stats, timeline snapshot, token usage) lives in
// TanStack Query. This store holds the LIVE render state the SSE stream folds
// into on top of that snapshot.
//
// Only the selected conversation owns live state. Switching replaces that
// state from an HTTP tail snapshot; inactive histories are not live caches.

"use client";

import { create } from "zustand";

import {
  foldEvent,
  mergeSnapshotWithStreaming,
  sortByItemId,
} from "./fold/timeline";
import { isEventForThread } from "./timeline";
import type { BackendTimelineItem, SystemEvent } from "./types";
import type { ConnectionState } from "./use-timeline";

/** One in-flight forced/auto compact run on the ACTIVE thread — the frontend
 *  half of the live run pair (wire: task #3323, this view: task #3324). The
 *  ISO timestamps drive the ticking "Compacting" block; `status` flips when
 *  the terminal role arrives. A live signal, not folded content: it never
 *  enters `items` — the summary item (matched by `compactId`) is the durable
 *  record, and its arrival retires the entry. */
export interface LiveCompact {
  compactId: string;
  /** ISO-8601 start; null when only the terminal role was seen (start lost
   *  in an SSE gap). */
  startedAt: string | null;
  mode: "request" | "auto" | null;
  /** null while the run is in flight. */
  status: "success" | "failure" | "replaced" | null;
  /** ISO-8601 finish; set exactly when `status` is. */
  finishedAt: string | null;
}

export interface TimelineState {
  items: BackendTimelineItem[];
  streamingCode: boolean;
  turnActive: boolean;
  connectionState: ConnectionState;
  tokenUsage: number;
  /** Reasoning-token portion of the last LLM call output (gemini/openai
   * reasoning models + Anthropic thinking). 0 for providers that do not
   * expose reasoning counts. */
  reasoningTokens: number;
  /** Model context window ceiling (max input tokens). Set from the
   * token-usage HTTP response; not carried by SSE events. */
  maxContextTokens: number;
  /** Per-agent soft (wind-down reminder) and hard (force-compact) thresholds,
   * both absolute token counts = a fraction of the model window. Set from the
   * token-usage HTTP response; per-model constants, so the live SSE token_usage
   * event leaves them untouched (like maxContextTokens). */
  softCompactTokens: number;
  hardCompactTokens: number;
  /** Active thread agent_id — used to validate SSE events; updated when switching agents */
  activeThreadId: number | null;

  /** Item ids the frontend has streamed this turn but that are not yet
   * committed (added on agent chat/code/reasoning *_start/*_delta, removed
   * once a snapshot commits them). On `cancelled` these are exactly the
   * abandoned generation's bubbles — the kernel commits nothing for a
   * cancelled generation — so they are dropped by id. Tracking the actual
   * streamed ids (rather than a msg_count boundary) is immune to a stale
   * boundary after SSE-missed snapshots, and never touches code_output. */
  streamingIds: Set<string>;

  /** Compact replacement is pending. Preserve the visible view until its
   * nonempty snapshot arrives; selection and reconnect reset this flag. */
  resetPending: boolean;

  /** Bumped every time a compact wholesale-replace lands on the ACTIVE
   * thread (the reset-window swap below, or a crossed-compact snapshot);
   * `compactReplaceAgent` names the thread it replaced. Consumers use it
   * as the edge for post-compact view work that must run ONCE per rewrite
   * — the compact-history re-attach loads the previous segment(s) after
   * this fires (task #3698). Inactive agents own no live state. */
  compactReplaceSeq: number;
  compactReplaceAgent: number | null;

  /** The active thread's in-flight compact run — drives the ticking
   * "Compacting" block. Set by `compact_started` / `compact_finished` for the
   * active thread only. Retired when the run's
   * summary item lands in `items` (matched by `compact_id`), replaced by a
   * newer run, and cleared on thread switch. */
  liveCompact: LiveCompact | null;

  /** Whether older items exist before the oldest currently-loaded item —
   * drives the scroll-up "load older" trigger. The timeline endpoint returns
   * only a tail window; this is its `has_more`. */
  hasMoreOlder: boolean;
  /** A scroll-up older-window fetch is in flight — guards against
   * re-triggering while loading + drives the top loading hint. */
  loadingOlder: boolean;

  /** How many times loadOlder has been called on the active thread —
   * drives exponential growth of the older-window fetch limit.
   * Reset when the selected conversation changes. */
  olderFetchCount: number;

  /** Force the viewport to bottom on selection or send. Reads/history paging
   * never bump this signal; ordinary content growth follows the sticky controller. */
  scrollToBottomRequest: number;
  /** Bump `scrollToBottomRequest` — called on send (the switch bump happens
   * inside `switchThread`). */
  requestScrollToBottom: () => void;

  /** SSE business-event handler — single entry point, replaces scattered setState calls */
  processSseEvent: (ev: SystemEvent) => void;

  /** SSE frame-batch entry point. Folds every event of ONE SSE frame inside a
   * single set() — one store notification + one render per frame instead of
   * one per event. The active-agent stream (`/api/system/all?agents=…`)
   * delivers batches at up to 10 frames/s; a busy agent's per-event path
   * otherwise turns each burst into a render/layout storm. Same reducer as
   * `processSseEvent` — batch and per-event paths can never diverge. */
  processSseEventBatch: (events: SystemEvent[]) => void;

  /** SSE connection event handler — banner / disconnect cleanup */
  processConnectionEvent: (ev: { type: ConnectionState }) => void;

  /** Merge after a reload snapshot — uses msg_count (authoritative, from the
   * GET /timeline response) to distinguish future vs committed partial.
   * msg_count = `len(state.messages)`. */
  reloadSnapshot: (snapshot: BackendTimelineItem[], msg_count: number, hasMoreOlder: boolean) => void;

  /** Replace the selected view atomically; inactive views retain no live state. */
  switchThread: (agentId: number | null, cached: BackendTimelineItem[] | null, hasMoreOlder: boolean) => void;

  /** Write the three context-window token fields atomically — input usage, the
   * reasoning portion, and the model's max input ceiling. The single gate for
   * token state, so `contextTokens` and `maxContextTokens` can never split-brain
   * across two renders (the old bug: `tokenUsage` through `processSseEvent` +
   * `maxContextTokens` through a bare `setState`). `useTokenUsage` calls
   * this for cold reset / hot restore / HTTP snapshot; live per-call SSE
   * `token_usage` still flows through `processSseEvent`, which leaves
   * `maxContextTokens` / `softCompactTokens` / `hardCompactTokens` (per-model
   * constants) untouched. */
  applyTokenUsage: (
    input: number,
    reasoning: number,
    maxContext: number,
    softCompact: number,
    hardCompact: number,
  ) => void;

  /** Mark an older-window fetch as started (scroll-up trigger). */
  beginLoadOlder: () => void;

  /** Prepend an older window fetched via scroll-up. Dedupes by item_id (an
   * SSE snapshot may already hold some), re-sorts, and sets hasMoreOlder
   * from the window's `has_more`. Clears loadingOlder. */
  prependOlder: (older: BackendTimelineItem[], hasMoreOlder: boolean) => void;

  /** Increment the older-fetch counter — called after a successful scroll-up
   * fetch so the next one doubles the limit (exponential growth). */
  incrementOlderFetchCount: () => void;

  /** Clear all partial flags when reload fails */
  clearPartialFlags: () => void;
}

// =============================================================
// Selected timeline reducer
// =============================================================

// Active history remains fully pageable. Switching releases the loaded view;
// it does not delete durable history or retain inactive live buckets.

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
function crossedUnseenCompact(
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
function retireLiveCompact(
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
function applySseEvent(state: TimelineState, ev: SystemEvent): Partial<TimelineState> {
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
        return {
          items: snapItems,
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

export const useTimelineStore = create<TimelineState>()((set, get) => ({
  items: [],
  streamingCode: false,
  turnActive: false,
  connectionState: "open",
  tokenUsage: 0,
  reasoningTokens: 0,
  maxContextTokens: 0,
  softCompactTokens: 0,
  hardCompactTokens: 0,
  activeThreadId: null,
  streamingIds: new Set(),
  resetPending: false,
  compactReplaceSeq: 0,
  compactReplaceAgent: null,
  liveCompact: null,
  hasMoreOlder: false,
  loadingOlder: false,
  olderFetchCount: 0,
  scrollToBottomRequest: 0,

  requestScrollToBottom: () => set((s) => ({ scrollToBottomRequest: s.scrollToBottomRequest + 1 })),

  processSseEvent: (ev) => {
    // agent_spawned / agent_updated belong to sidebar state (TanStack
    // Query cache), not the timeline. The root fold (the single
    // cache writer) handles them; the timeline store ignores them outright.
    if (ev.role === "agent_spawned" || ev.role === "agent_updated") return;
    set((s) => applySseEvent(s, ev));
  },

  processSseEventBatch: (events) => {
    let changed = false;
    const merged: Partial<TimelineState> = {};
    let working = get();
    for (const ev of events) {
      if (ev.role === "agent_spawned" || ev.role === "agent_updated") continue;
      const patch = applySseEvent(working, ev);
      if (Object.keys(patch).length === 0) continue;
      changed = true;
      Object.assign(merged, patch);
      working = { ...working, ...patch };
    }
    if (!changed) return;
    // One set() per frame: every event's fold already applied to `working`;
    // `merged` carries the cumulative patch. Synchronous — no other set()
    // can interleave between get() above and this commit.
    set(merged);
  },

  processConnectionEvent: (ev) => {
    set({ connectionState: ev.type });
    if (ev.type === "closed") {
      // SSE disconnect = streaming interrupted. Keep the partial flag
      // (showing the content as "..." ellipsis remains reasonable), add
      // an interrupted flag so the timeline renders an extra
      // "streaming interrupted" hint that distinguishes "message simply
      // ends here" from "disconnect, content may be incomplete". After
      // reconnect, SSE keeps pushing deltas and naturally flips
      // interrupted off (the delta reducer does not carry this flag).
      set((s) => ({
        streamingCode: false,
        turnActive: false,
        items: s.items.some((it) => it.partial)
          ? s.items.map((it) =>
              it.partial && !it.interrupted ? { ...it, interrupted: true } : it,
            )
          : s.items,
      }));
    } else if (ev.type === "open") {
      // Reconnected; clear the interrupted flag — the previously
      // partial items will keep appending via deltas, so the "interrupted"
      // hint no longer applies.
      set((s) => ({
        items: s.items.some((it) => it.interrupted)
          ? s.items.map((it) => (it.interrupted ? { ...it, interrupted: false } : it))
          : s.items,
        // Reopening requests a fresh snapshot and releases the compact guard.
        // The existing wire contract cannot prove its commit order.
        resetPending: false,
      }));
    }
  },

  reloadSnapshot: (snapshot, msg_count, hasMoreOlder) => {
    set((s) => {
      // A compact changes item coordinates. During the reset window only
      // a snapshot showing that rewrite can replace the old view.
      const crossedCompact = crossedUnseenCompact(s.items, snapshot);
      if (s.resetPending && !crossedCompact) {
        return { hasMoreOlder };
      }
      const snapshotIds = new Set(snapshot.map((it) => it.item_id));
      const merged = mergeSnapshotWithStreaming(
        crossedCompact ? [] : s.items,
        snapshot,
        msg_count,
        s.streamingIds,
      );
      return {
        // committed ids drop out of streamingIds; still-streaming ones stay
        streamingIds: new Set([...s.streamingIds].filter((id) => !snapshotIds.has(id))),
        items: merged,
        hasMoreOlder,
        resetPending: crossedCompact ? false : s.resetPending,
        // Reconnect can discover the rewrite without any compact SSE event.
        // A cold snapshot seeds a view; only an existing view needs retention.
        compactReplaceSeq: crossedCompact && s.items.length > 0 ? s.compactReplaceSeq + 1 : s.compactReplaceSeq,
        compactReplaceAgent: crossedCompact && s.items.length > 0 ? s.activeThreadId : s.compactReplaceAgent,
        liveCompact: retireLiveCompact(s.liveCompact, merged),
      };
    });
  },

  switchThread: (agentId, cached, hasMoreOlder) => {
    set((s) => ({
      activeThreadId: agentId,
      items: cached ?? [],
      streamingIds: new Set(),
      streamingCode: false,
      turnActive: false,
      hasMoreOlder: cached !== null && hasMoreOlder,
      olderFetchCount: 0,
      resetPending: false,
      liveCompact: null,
      tokenUsage: 0,
      reasoningTokens: 0,
      maxContextTokens: 0,
      softCompactTokens: 0,
      hardCompactTokens: 0,
      loadingOlder: false,
      scrollToBottomRequest: s.scrollToBottomRequest + 1,
    }));
  },

  applyTokenUsage: (input, reasoning, maxContext, softCompact, hardCompact) =>
    set({
      tokenUsage: input,
      reasoningTokens: reasoning,
      maxContextTokens: maxContext,
      softCompactTokens: softCompact,
      hardCompactTokens: hardCompact,
    }),

  beginLoadOlder: () => set({ loadingOlder: true }),

  prependOlder: (older, hasMoreOlder) => {
    set((s) => {
      const existing = new Set(s.items.map((it) => it.item_id));
      const fresh = older.filter((it) => !existing.has(it.item_id));
      if (fresh.length === 0 && hasMoreOlder) {
        // The page added nothing yet claims older history still exists: the
        // gateway returned only standing context it could not cross (a
        // version mix — an older gateway does not recognize the re-attached
        // head notes as crossing material). Stop paging instead of looping
        // on the same cursor forever.
        return { hasMoreOlder: false, loadingOlder: false };
      }
      const merged = fresh.length ? sortByItemId([...fresh, ...s.items]) : s.items;
      return {
        items: merged,
        hasMoreOlder,
        loadingOlder: false,
      };
    });
  },

  incrementOlderFetchCount: () => set((s) => ({ olderFetchCount: s.olderFetchCount + 1 })),

  clearPartialFlags: () => {
    set((s) => ({
      items: s.items.some((it) => it.partial)
        ? s.items.map((it) => (it.partial ? { ...it, partial: false } : it))
        : s.items,
    }));
  },
}));
