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
// Per-thread timeline caching (R1/R2/R3): the all-events SSE stream
// (`/api/system/all`) carries EVERY agent's events. The ACTIVE thread's
// timeline state lives in the top-level fields (items / turnActive /
// streamingCode / streamingIds / hasMoreOlder); every INACTIVE (parked)
// thread's state lives in `threads: Map<agentId, ThreadTimelineState>`.
// A thread is in exactly one place — top-level while active, the map while
// parked — and `switchThread` is the single mover (park the outgoing thread,
// unpark the incoming one, delete the active id from the map). So background
// events for a switched-away thread fold into its parked bucket instead of
// being dropped (R3), and switching back restores the live-folded state
// instantly (R2) rather than a stale HTTP snapshot. Token fields are NOT
// per-thread here — they keep their own per-thread cache in React Query
// (`["token-usage", agentId]`) via useTokenUsage; duplicating them into the
// bucket would be a second per-thread token source.

"use client";

import { create } from "zustand";

import {
  foldEvent,
  mergeSnapshotWithStreaming,
  sortByItemId,
} from "./fold/timeline";
import type { ThreadTimelineState } from "./fold/timeline";
import { isEventForThread } from "./timeline";
import type { BackendTimelineItem, SystemEvent } from "./types";
import type { ConnectionState } from "./use-timeline";

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

  /** Parked (inactive) threads' foldable timeline state, keyed by agent_id.
   * The ACTIVE thread is never in here (its state lives in the top-level
   * fields above); `switchThread` parks the outgoing thread and unparks the
   * incoming one. Background SSE events for a parked thread fold into its
   * bucket (see `processSseEvent`) so switching back is instant (R2/R3).
   * LRU-capped at MAX_PARKED_THREADS — a chatty background thread never grows
   * this unbounded. Ephemeral (not persisted). */
  threads: Map<number, ThreadTimelineState>;
  /** Item ids the frontend has streamed this turn but that are not yet
   * committed (added on agent chat/code/reasoning *_start/*_delta, removed
   * once a snapshot commits them). On `cancelled` these are exactly the
   * abandoned generation's bubbles — the kernel commits nothing for a
   * cancelled generation — so they are dropped by id. Tracking the actual
   * streamed ids (rather than a msg_count boundary) is immune to a stale
   * boundary after SSE-missed snapshots, and never touches code_output. */
  streamingIds: Set<string>;

  /** Agent ids whose compact_done arrived while the thread was PARKED (no
   * active reset window to carry the flag). On switch-back the reset window
   * is armed for the thread (whatever it seeds — the parked bucket, or the
   * React Query cache which may hold the lagging pre-compact snapshot — is
   * replaced wholesale by the first post-compact snapshot instead of being
   * keep-merged, so the old history cannot resurrect). Cleared when the
   * reset completes (first non-empty snapshot), on switch-back (consumed),
   * or on reconnect. The active-thread flag lives in `resetPending`; this
   * set is the parked-thread counterpart. */
  compactedThreadIds: ReadonlySet<number>;

  /** Compact-reset window flag (active thread only). Set when compact_done
   * arrives: the whole history was rewritten (shrink), keep-all merging
   * would resurrect pre-compact items, and a GET fired by the compact
   * invalidation may read a lagging (pre-compact) checkpoint. While set,
   * GET merges are dropped; the NEXT timeline_snapshot SSE event (SSE
   * ordering guarantees it is post-compact — the compacting turn's
   * init_context enter) replaces the thread's items wholesale and clears
   * the flag. Cleared on SSE reconnect (GET becomes trusted again) and on
   * thread switch (per-thread flag). */
  resetPending: boolean;

  /** Whether older items exist before the oldest currently-loaded item —
   * drives the scroll-up "load older" trigger. The timeline endpoint returns
   * only a tail window; this is its `has_more`. */
  hasMoreOlder: boolean;
  /** A scroll-up older-window fetch is in flight — guards against
   * re-triggering while loading + drives the top loading hint. */
  loadingOlder: boolean;

  /** How many times loadOlder has been called on the active thread —
   * drives exponential growth of the older-window fetch limit.
   * Tracks the same value as ThreadTimelineState.olderFetchCount for the
   * active thread; travels with the thread through park/unpark. */
  olderFetchCount: number;

  /** Monotonic "force the viewport to the bottom + re-stick" signal — the
   * SINGLE force-scroll trigger the timeline honors. Bumped for exactly the
   * two moments a scroll-to-bottom is unconditional: an agent switch
   * (`switchThread`, bumped in the same `set()` that installs the new
   * thread's items, so the timeline's layout effect pins AFTER the new items
   * are in the DOM — no stale-bottom race) and a send
   * (`requestScrollToBottom`). Content growth is NOT a bump: the timeline's
   * ResizeObserver + the controller's latched sticky flag handle streamed
   * growth on their own. Deliberately not bumped by `reloadSnapshot` /
   * `prependOlder` — a mid-stream snapshot refresh or a scroll-up load-older
   * must never yank the viewport to the bottom. This replaces the old
   * dual trigger (parent-owned `scrollToken` + a separate `activeThreadId`
   * effect), which double-pinned on switch and pinned before items loaded. */
  scrollToBottomRequest: number;
  /** Bump `scrollToBottomRequest` — called on send (the switch bump happens
   * inside `switchThread`). */
  requestScrollToBottom: () => void;

  /** SSE business-event handler — single entry point, replaces scattered setState calls */
  processSseEvent: (ev: SystemEvent) => void;

  /** SSE frame-batch entry point. Folds every event of ONE SSE frame inside a
   * single set() — one store notification + one render per frame instead of
   * one per event. The all-events broadcast (`/api/system/all`) delivers every
   * agent's events batched at up to 25 frames/s; when the fleet is busy (the
   * moment a user opens a live shell view of an active agent) the per-event
   * path turned each burst into a render/layout storm. Same reducer as
   * `processSseEvent` — batch and per-event paths can never diverge. */
  processSseEventBatch: (events: SystemEvent[]) => void;

  /** SSE connection event handler — banner / disconnect cleanup */
  processConnectionEvent: (ev: { type: ConnectionState }) => void;

  /** Merge after a reload snapshot — uses msg_count (authoritative, from the
   * GET /timeline response) to distinguish future vs committed partial.
   * msg_count = `len(state.messages)`. */
  reloadSnapshot: (snapshot: BackendTimelineItem[], msg_count: number, hasMoreOlder: boolean) => void;

  /** Atomic agent switch — the SINGLE writer of `activeThreadId` AND the single
   * mover between top-level (active) and the `threads` map (parked). In one
   * `set()`: park the outgoing active thread's timeline state into the map,
   * then load the incoming thread. Load precedence: (1) a PARKED bucket wins —
   * it holds background-folded events newer than any HTTP snapshot (R2/R3);
   * (2) else seed from `cached` (React Query already holds a snapshot → hot
   * restore, no flash); (3) else cold (empty until the fetch lands). The item
   * swap, turn/streaming flags, `streamingIds`, the older-window flags, the
   * token reset, and the force-scroll bump all move together — so the SSE gate
   * (`activeThreadId`) and the loaded items can never disagree mid-switch.
   * Token fields reset to cold here; `useTokenUsage` restores the hot value
   * through `applyTokenUsage` in the same commit (React batches → no flicker).
   * The map is LRU-capped after parking. */
  switchThread: (agentId: number, cached: BackendTimelineItem[] | null, hasMoreOlder: boolean) => void;

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
// Per-thread timeline reducer + LRU
// =============================================================

/** How many parked (inactive) threads keep their live-folded state. Beyond
 *  this, the least-recently-parked thread is evicted; revisiting it cold-fetches
 *  (its React Query snapshot may still hot-restore within gcTime). Bounds the
 *  map so a fleet of chatty background agents can't grow it unbounded. */
const MAX_PARKED_THREADS = 32;
export const MAX_TIMELINE_ITEMS = 6_000;

/** Evict least-recently-parked buckets until the map is within the cap. A Map
 *  preserves insertion order, and `switchThread` re-`set`s the just-parked
 *  thread last (most recent), so the oldest live at the front. Mutates in place
 *  — callers pass a fresh copy. */
function evictLruThreads(threads: Map<number, ThreadTimelineState>): void {
  while (threads.size > MAX_PARKED_THREADS) {
    const oldest = threads.keys().next().value;
    if (oldest === undefined) break;
    threads.delete(oldest);
  }
}


/**
 * The per-event reducer shared by `processSseEvent` and
 * `processSseEventBatch` — one event, one state, one partial to merge
 * ({} = nothing changed). Pure: both entry points route through it, so the
 * batch path can never diverge from the per-event path.
 */
function applySseEvent(state: TimelineState, ev: SystemEvent): Partial<TimelineState> {
  // token_usage is a thread's own concern but NOT part of ThreadTimelineState
  // — the token fields are cached per-thread in React Query and mirrored to
  // the top-level active-thread fields. Write the active thread's token
  // fields; a parked thread's token event is dropped (its value is restored
  // from React Query on switch-back). agent_id=0 is a system reset (passes
  // isEventForThread), which writes tokenUsage=0 on the active thread as before.
  if (ev.role === "token_usage") {
    return isEventForThread(ev, state.activeThreadId)
      ? { tokenUsage: ev.input_tokens, reasoningTokens: ev.reasoning_tokens ?? 0 }
      : {};
  }

  // ACTIVE thread (or agent_id=0 system signal): fold into the top-level
  // fields. This is the rendered thread, so its updates drive the UI.
  if (isEventForThread(ev, state.activeThreadId)) {
    // compact_done = the whole history was rewritten (shrink). keep-all
    // merging would resurrect pre-compact items, and a GET fired during
    // the window may read a lagging pre-compact checkpoint — so arm the
    // reset window: the first NON-EMPTY timeline_snapshot (SSE ordering
    // guarantees it is post-compact — the rebuilt head renders at the
    // next node enter) replaces the items wholesale. The pre-compact
    // items stay visible until that swap: clearing them here made the
    // context panel flash blank for the whole window (sub-second locally,
    // seconds on remote machines — the "context UI doesn't refresh after
    // compact" report).
    if (ev.role === "compact_done") {
      return {
        streamingIds: new Set(),
        // Old foldEvent treated compact_done as a code-end (streamingCode
        // false, turnActive untouched); preserve that flag behavior.
        streamingCode: false,
        resetPending: true,
      };
    }
    // First snapshot inside the reset window: overall replace, not merge.
    // The backend publishes a full-window snapshot on the post-compact
    // node enter (its cursor is past the shrunk history), so this is a
    // complete, race-free (in-memory) view of the new history. An EMPTY
    // snapshot (a wiped-but-not-yet-rebuilt history, e.g. the
    // post-REMOVE_ALL init_context enter on an older backend) is skipped
    // — replacing with [] would blank the panel before the real history
    // arrives.
    if (state.resetPending && ev.role === "timeline_snapshot") {
      const snapItems = ev.items as unknown as BackendTimelineItem[];
      if (snapItems.length === 0) return {};
      return {
        items: snapItems,
        streamingIds: new Set(),
        resetPending: false,
      };
    }
    const next = foldEvent(
      {
        items: state.items,
        streamingIds: state.streamingIds,
        streamingCode: state.streamingCode,
        turnActive: state.turnActive,
        hasMoreOlder: state.hasMoreOlder,
        olderFetchCount: state.olderFetchCount,
        // The active thread's reset window state rides in the fold input
        // so a parked fold inside foldEvent keeps it (foldEvent spreads t).
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
    };
  }

  // PARKED (inactive) thread with a live bucket: fold in the background so
  // switching back is instant (R3). Non-parked, non-active threads have no
  // bucket → drop (unchanged from the old isEventForThread gate). This
  // never re-renders the active view: no render selector reads `threads`.
  const parked = state.threads.get(ev.agent_id);
  const threads = new Map(state.threads);
  if (ev.role === "compact_done") {
    // compact_done rewrites the whole history (a shrink); foldEvent is
    // built for growth and can't reconcile it. Mark the thread so a
    // switch-back seeds cold with the reset window armed (the React Query
    // cache may hold the lagging pre-compact snapshot) — the first
    // post-compact snapshot then replaces wholesale. A live bucket keeps
    // its items (still visible on switch-back) with the flag set; a
    // bucketless thread is covered by the marker alone.
    const compactedThreadIds = new Set(state.compactedThreadIds);
    compactedThreadIds.add(ev.agent_id);
    if (parked) {
      threads.set(ev.agent_id, {
        ...parked,
        streamingIds: new Set(),
        streamingCode: false,
        resetPending: true,
      });
    }
    return { threads, compactedThreadIds };
  }
  if (parked) {
    // Parked reset window: same wholesale-replace rule as the active
    // thread — the first non-empty post-compact snapshot replaces the
    // bucket and clears the marker.
    if (parked.resetPending && ev.role === "timeline_snapshot") {
      const snapItems = ev.items as unknown as BackendTimelineItem[];
      if (snapItems.length === 0) return {};
      const compactedThreadIds = new Set(state.compactedThreadIds);
      compactedThreadIds.delete(ev.agent_id);
      threads.set(ev.agent_id, {
        ...parked,
        items: snapItems,
        streamingIds: new Set(),
        resetPending: false,
      });
      return { threads, compactedThreadIds };
    }
    threads.set(ev.agent_id, foldEvent(parked, ev));
  }
  // Bucketless compact marker: compact_done arms the reset window for a
  // thread with NO parked bucket via `compactedThreadIds` (a later
  // switch-back seeds cold + resetPending, so the lagging pre-compact
  // cache cannot be keep-merged back in). The reset window's whole job is
  // done by the FIRST timeline_snapshot after compact_done — SSE order
  // guarantees it is the full post-compact snapshot (the backend emits it
  // from in-memory state on the post-compact node enter). For a parked
  // thread with a bucket that snapshot is folded into the bucket and
  // clears the window there; for a BUCKLESS thread it is dropped, so the
  // marker would otherwise survive until a switch-back and arm a STALE
  // window: the HTTP snapshot is dropped inside it, and the first
  // *incremental* snapshot (the full one already passed, the agent may
  // still be streaming) replaces the fresh seed wholesale — the timeline
  // ends up showing only the tail ("只显示最后一个 detail block，之前
  // 所有消息不触发加载", Task #994). Consume the marker here: the full
  // snapshot happened, nothing is left to protect.
  if (ev.role === "timeline_snapshot" && state.compactedThreadIds.has(ev.agent_id)) {
    const nextCompacted = new Set(state.compactedThreadIds);
    nextCompacted.delete(ev.agent_id);
    return { threads, compactedThreadIds: nextCompacted };
  }
  return { threads };
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
  threads: new Map(),
  streamingIds: new Set(),
  compactedThreadIds: new Set(),
  resetPending: false,
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
        // The SSE stream is the trusted source again — and the reconnect
        // invalidates the timeline query, whose GET must now be allowed to
        // apply (a compact that happened while disconnected has long since
        // committed its checkpoint by now). Clear the reset window.
        resetPending: false,
        compactedThreadIds: new Set(),
      }));
    }
  },

  reloadSnapshot: (snapshot, msg_count, hasMoreOlder) => {
    set((s) => {
      // Inside a compact-reset window a GET may read a lagging pre-compact
      // checkpoint (compact commits asynchronously; compact_done fires before
      // the commit lands) — never merge it, keep-all would resurrect the old
      // history. The post-compact SSE snapshot (memory-rendered, race-free)
      // replaces the thread; until then the thread shows empty. hasMoreOlder
      // is still refreshed — the truncation is real either way.
      if (s.resetPending) {
        return { hasMoreOlder };
      }
      const snapshotIds = new Set(snapshot.map((it) => it.item_id));
      return {
        // committed ids drop out of streamingIds; still-streaming ones stay
        streamingIds: new Set([...s.streamingIds].filter((id) => !snapshotIds.has(id))),
        items: mergeSnapshotWithStreaming(s.items, snapshot, msg_count, s.streamingIds),
        hasMoreOlder,
      };
    });
  },

  switchThread: (agentId, cached, hasMoreOlder) => {
    // Agent switch. In ONE set(): (1) park the outgoing active thread's
    // timeline state into the map; (2) load the incoming thread — a PARKED
    // bucket wins (it holds background-folded events, fresher than any HTTP
    // snapshot: R2/R3), else seed from `cached` (hot React Query snapshot →
    // instant restore, no flash), else cold (empty until the fetch lands).
    // activeThreadId + the item swap move together, so the SSE gate and the
    // loaded items never disagree mid-switch. Bumping scrollToBottomRequest in
    // the same set() pins the timeline to the new thread's bottom AFTER its
    // items are in the DOM (the layout effect reads the post-swap DOM). Token
    // fields go cold here; useTokenUsage restores the hot value through
    // applyTokenUsage in the same commit (React batches → no flicker) — tokens
    // are NOT parked (React Query is their per-thread cache).
    set((s) => {
      const threads = new Map(s.threads);
      // Park the outgoing active thread (its live top-level state).
      if (s.activeThreadId != null) {
        threads.set(s.activeThreadId, {
          items: s.items,
          streamingIds: s.streamingIds,
          streamingCode: s.streamingCode,
          turnActive: s.turnActive,
          hasMoreOlder: s.hasMoreOlder,
          olderFetchCount: s.olderFetchCount,
          // Carry the reset window into the parked bucket: a compact that
          // started while the thread was active keeps its flag so the first
          // post-compact snapshot replaces wholesale after switch-back too.
          resetPending: s.resetPending,
        });
      }
      // The active thread lives in the top-level fields, never in the map.
      const parked = threads.get(agentId);
      threads.delete(agentId);
      // A thread whose compact_done arrived while parked (marker set) keeps
      // the reset window armed on switch-back: whatever it seeds (the parked
      // bucket, or the cached snapshot — possibly the lagging pre-compact
      // one) is replaced wholesale by the first post-compact snapshot instead
      // of being keep-merged, so the old history cannot resurrect. Consume
      // the marker here; a later switch-back (after the snapshot has already
      // folded into the bucket or refreshed the cache) is clean.
      const compactedThreadIds = new Set(s.compactedThreadIds);
      const wasCompacted = compactedThreadIds.delete(agentId);
      // Re-inserting the outgoing thread above moved it to most-recent; evict
      // the least-recently-parked beyond the cap (the just-loaded thread is
      // already removed, so it can't be evicted).
      evictLruThreads(threads);
      const loaded: ThreadTimelineState = parked ?? {
        items: cached ?? [],
        streamingIds: new Set(),
        streamingCode: false,
        turnActive: false,
        hasMoreOlder: cached ? hasMoreOlder : false,
        olderFetchCount: 0,
        resetPending: wasCompacted,
      };
      return {
        threads,
        activeThreadId: agentId,
        items: loaded.items,
        streamingIds: loaded.streamingIds,
        streamingCode: loaded.streamingCode,
        turnActive: loaded.turnActive,
        hasMoreOlder: loaded.hasMoreOlder,
        olderFetchCount: loaded.olderFetchCount,
        // Preserve the live connection state: switchThread only swaps the
        // thread — stamping "open" here would wrongly clear the
        // all-events-stream disconnect banner until the next connection event
        // (the stream may actually be reconnecting right now).
        connectionState: s.connectionState,
        resetPending: parked ? parked.resetPending : wasCompacted,
        compactedThreadIds,
        tokenUsage: 0,
        reasoningTokens: 0,
        maxContextTokens: 0,
        softCompactTokens: 0,
        hardCompactTokens: 0,
        loadingOlder: false,
        scrollToBottomRequest: s.scrollToBottomRequest + 1,
      };
    });
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
      const merged = fresh.length ? sortByItemId([...fresh, ...s.items]) : s.items;
      const reachedItemLimit = merged.length >= MAX_TIMELINE_ITEMS;
      return {
        // Retain the newest end of the chronological list. If a fetched page
        // crosses the cap, its farthest-back items are the ones discarded;
        // current conversation content is never evicted by history loading.
        items:
          merged.length > MAX_TIMELINE_ITEMS
            ? merged.slice(merged.length - MAX_TIMELINE_ITEMS)
            : merged,
        hasMoreOlder: reachedItemLimit ? false : hasMoreOlder,
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
