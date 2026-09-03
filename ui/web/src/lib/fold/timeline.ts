// R4 layer 1 — timeline-domain fold (Task #1024, design §5.2).
//
// The pure reducer for ONE thread's streaming timeline. Extracted from
// timeline.ts (applySystemEvent + mergeSnapshotWithStreaming + helpers) and
// timeline-store.ts (foldEvent + ThreadTimelineState): the fold lives in
// lib/fold/, the store keeps only thread-cache management (parking/LRU/switch).
//
// Input: initial BackendTimelineItem list from GET /timeline (the
// LangGraph state.messages view; the inbound_messages table only
// anchors timestamps); SSE delta events folded in.
// Output: monotonically-appended BackendTimelineItem[]. The frontend
// renders directly, no hidden state.
//
// **Stable id protocol** (2026-05-06): the backend and streaming SSE compute
// `item_id = f"{msg_idx}.{block_idx}"` for current items. Cold-loaded compact
// history uses `s<rank>.<boundary_checkpoint_id>.<msg_idx>.<block_idx>` and
// never participates in the live partial merge. The frontend reducer and
// merge both index by the complete id — so snapshot and
// streaming items align at the same logical position, merge is a direct
// Map<id, item>, no more trail walks + timestamp heuristics (old
// race bugs were rooted in timestamps not distinguishing "new streaming"
// from "commit from the previous reload").
//
// applySystemEvent(items, ev) → handles all SSE events (chat_*/code_*/
//   reasoning_*/exec_*/compact_*/cancelled/error/inbound_arrived/
//   inbound_committed/llm_done). New items are appended at the tail —
//   the single SSE connection (EventSource over TCP) delivers frames in
//   publish order and item_ids are monotonically increasing (msg_idx =
//   len(state.messages) at stream time), so arrival order IS business
//   order in practice. NEW items are still insert-sorted by item_id
//   (sortByItemId — same comparator as mergeSnapshotWithStreaming) rather
//   than blindly tail-appended: creation happens once per item (never per
//   chunk), and a genuinely out-of-order arrival (mid-stream join that
//   missed a *_start, reconnect-window resend) would otherwise sit
//   misplaced until the next snapshot merge. Matched-id deltas update in
//   place. (The pre-2026-05-10 multi-connection architecture that first
//   motivated insert-sort is gone; this is pure defensive consistency.)
//
// mergeSnapshotWithStreaming(prev, snapshot, msg_count, streamingIds) →
//   the incremental-snapshot design (2026-08): each graph node enter the
//   agent publishes a timeline_snapshot carrying ONLY the messages
//   committed since its last snapshot (full-window snapshots happen on
//   process start / after compact / at turn end — the frontend cannot
//   and need not tell them apart). Two rules + one narrow cleanup:
//   (1) item_id in snapshot → replace with the commit version;
//   (2) every prev item NOT in the snapshot is kept — committed items,
//       scroll-loaded history, ephemeral markers (they carry "this
//       happened" info) and the system-prompt item all stay verbatim;
//   (3) narrow stale rule: a prev item still flagged `partial` (an
//       error-interrupted generation never committed) is dropped unless
//       it is the single future position (`msg_idx == msg_count`, still
//       streaming) or still tracked in `streamingIds` (a cancel may drop
//       it by id). Compaction is NOT reconciled here — the store hard-
//       resets on compact_done (see timeline-store.ts).
//
// This module is intentionally self-contained (only ../types): timeline.ts
// re-exports the helpers for its historical importers, so nothing here may
// import ../timeline (cycle).

import type { BackendTimelineItem, SystemEvent } from "../types";
/**
 * Parse a backend item_id (format: "{msg_idx}.{block_idx}") into a
 * sortable tuple. Unparseable inputs return null — ephemeral markers
 * (`_marker.*`) and anything that's not "<number>.<number>" go through
 * the null path and land at the end.
 *
 * `_marker.*` is filtered out by `Number("_marker") = NaN` failing
 * Number.isFinite; no separate startsWith guard needed (would duplicate
 * the split + finite check).
 */
export function parseItemId(itemId: string): [number, number] | null {
  const parts = itemId.split(".");
  if (parts.length !== 2) return null;
  const msgIdx = Number(parts[0]);
  const blockIdx = Number(parts[1]);
  if (!Number.isFinite(msgIdx) || !Number.isFinite(blockIdx)) return null;
  return [msgIdx, blockIdx];
}

export interface ItemIdParts {
  /** 0 is the live/current segment; positive ranks are compact boundaries. */
  rank: number;
  checkpointId: string | null;
  msg: number;
  block: number;
}

/**
 * Parse both stable-id wire shapes without widening parseItemId's current-
 * segment-only contract. Historical ids carry their exact boundary identity;
 * rank is only their presentational order and may shift after another compact.
 */
export function parseItemIdParts(itemId: string): ItemIdParts | null {
  const current = parseItemId(itemId);
  if (current !== null) {
    const [msg, block] = current;
    if (!Number.isInteger(msg) || !Number.isInteger(block) || msg < 0 || block < 0) {
      return null;
    }
    return { rank: 0, checkpointId: null, msg, block };
  }

  const parts = itemId.split(".");
  if (parts.length !== 4) return null;
  const [segment, checkpointId, msgPart, blockPart] = parts;
  if (!/^s[1-9]\d*$/.test(segment) || checkpointId.length === 0) return null;
  const rank = Number(segment.slice(1));
  const msg = Number(msgPart);
  const block = Number(blockPart);
  if (
    !Number.isSafeInteger(rank) ||
    !Number.isSafeInteger(msg) ||
    !Number.isSafeInteger(block) ||
    msg < 0 ||
    block < 0
  ) {
    return null;
  }
  return { rank, checkpointId, msg, block };
}

/**
 * Stable insert for a NEW item by its numeric item_id — used by the
 * delta-creation paths. Unlike sortByItemId (which moves ephemeral
 * _marker.* items to the end), this only finds the new item's slot among
 * the numeric items and leaves every EXISTING item in place: markers keep
 * their inline position in the flow, and a genuinely out-of-order new id
 * (mid-stream join, reconnect-window resend) still lands in business
 * order. Ephemeral new items append at the tail (their own paths place
 * them).
 */
function insertItemById(items: BackendTimelineItem[], newItem: BackendTimelineItem): BackendTimelineItem[] {
  const parsed = parseItemId(newItem.item_id);
  if (parsed === null) return [...items, newItem];
  let idx = items.length;
  for (let i = 0; i < items.length; i++) {
    const p = parseItemId(items[i].item_id);
    if (p === null) continue; // markers keep their place — never sort past them
    if (p[0] > parsed[0] || (p[0] === parsed[0] && p[1] > parsed[1])) {
      idx = i;
      break;
    }
  }
  return [...items.slice(0, idx), newItem, ...items.slice(idx)];
}

/**
 * Sort historical ranks oldest-first (sN ... s1), then the current segment;
 * within each segment sort by msg/block. Ephemeral markers land at the end.
 */
export function sortByItemId(items: BackendTimelineItem[]): BackendTimelineItem[] {
  return [...items].sort((a, b) => {
    const pa = parseItemIdParts(a.item_id);
    const pb = parseItemIdParts(b.item_id);
    // null (ephemeral) goes last; both null → 0 preserves relative order
    if (pa === null) return pb === null ? 0 : 1;
    if (pb === null) return -1;
    // Higher ranks are older and render first; current rank 0 renders last.
    if (pa.rank !== pb.rank) return pb.rank - pa.rank;
    if (pa.msg !== pb.msg) return pa.msg - pb.msg;
    return pa.block - pb.block;
  });
}

// =============================================================
// SSE event -> items list transformation
// =============================================================

// Find / create an item — indexed by item_id rather than array tail,
// so out-of-order scenarios like chat followed by reasoning_delta
// (Anthropic occasionally interleaves thinking + text) do not misfile
// the delta onto a previous different block.
//
// Matched id → append delta to payload, preserve partial flag (start-
// created items are not partial; delta-only-bootstrap creates with
// partial=true as fallback).
// No matching id → create a new item, insert-sorted by item_id (fixes
// SSE out-of-order arrival bug), partial=true (fallback for users who
// joined mid-stream and missed *_start).
function appendDeltaById(
  items: BackendTimelineItem[],
  itemId: string,
  kind: "agent_chat" | "agent_code" | "agent_reasoning" | "code_output",
  delta: string,
): BackendTimelineItem[] {
  const idx = items.findIndex((it) => it.item_id === itemId);
  if (idx >= 0) {
    const updated: BackendTimelineItem = {
      ...items[idx],
      payload: items[idx].payload + delta,
    };
    return [...items.slice(0, idx), updated, ...items.slice(idx + 1)];
  }
  const newItem: BackendTimelineItem = {
    item_id: itemId,
    kind,
    source: null,
    payload: delta,
    created_at: new Date().toISOString(),
    inbound_id: null,
    // Streaming items are never system-note markers, so the flag is unused by
    // their renderers; true keeps the honest "always present" contract.
    show_timestamp: true,
    partial: true,
    // Start the live "Thinking for Xs" clock the moment the first reasoning
    // fragment lands (used when this delta arrives before its start event).
    ...(kind === "agent_reasoning" ? { reasoningStartedAt: Date.now() } : {}),
    // Start the live "writing code for Xs" clock the moment the first code
    // fragment lands (used when this delta arrives before its start event).
    ...(kind === "agent_code" ? { codeStartedAt: Date.now() } : {}),
    // Start the live "Running for Xs" clock the moment the first exec output
    // chunk lands.
    ...(kind === "code_output" ? { execStartedAt: Date.now() } : {}),
  };
  // Stable insert by item_id: a brand-new id is NOT guaranteed to be the
  // largest — deltas can arrive out of order (mid-stream join that missed
  // *_start, reconnect-window resend), and this pure-delta path never goes
  // through the snapshot merge's re-sort, so a tail append would leave the
  // item misplaced until the next snapshot. insertItemById keeps existing
  // items (including inline markers) exactly where they are.
  return insertItemById(items, newItem);
}

// *_start creates an empty item (no partial flag), insert-sorted by
// item_id. If id already exists (commit version already in items after
// reload, then a *_start arrives — uncommon), no-op, don't recreate.
function createStartItem(
  items: BackendTimelineItem[],
  itemId: string,
  kind: "agent_chat" | "agent_code" | "agent_reasoning",
): BackendTimelineItem[] {
  if (items.some((it) => it.item_id === itemId)) return items;
  return insertItemById(items, {
    item_id: itemId,
    kind,
    source: null,
    payload: "",
    created_at: new Date().toISOString(),
    inbound_id: null,
    show_timestamp: true,
    // reasoning_start: anchor the live "Thinking for Xs" clock now.
    ...(kind === "agent_reasoning" ? { reasoningStartedAt: Date.now() } : {}),
    // code_start: anchor the live "writing code for Xs" clock now.
    ...(kind === "agent_code" ? { codeStartedAt: Date.now() } : {}),
  });
}

function upsertItemById(
  items: BackendTimelineItem[],
  next: BackendTimelineItem,
): BackendTimelineItem[] {
  const idx = items.findIndex((it) => it.item_id === next.item_id);
  if (idx >= 0) {
    return [...items.slice(0, idx), next, ...items.slice(idx + 1)];
  }
  // Stable insert by item_id — same out-of-order rationale as
  // appendDeltaById (this path also skips the snapshot merge's re-sort);
  // existing items and markers keep their positions.
  return insertItemById(items, next);
}

// Ephemeral system_marker uses a `_marker.*` id (no stable backend id;
// all one-shot client markers). Distinguishes stable ids (also in the
// snapshot) from transient UI state, so merge / replace logic can act
// precisely on just the transient portion.
const EPHEMERAL_MARKER_PREFIX = "_marker.";

function isEphemeralMarker(it: BackendTimelineItem): boolean {
  return it.item_id.startsWith(EPHEMERAL_MARKER_PREFIX);
}

function isExecStartMarker(it: BackendTimelineItem): boolean {
  return (
    isEphemeralMarker(it) &&
    it.kind === "system_marker" &&
    it.payload === "exec_start"
  );
}

function removeExecStartMarkers(items: BackendTimelineItem[]): BackendTimelineItem[] {
  return items.filter((it) => !isExecStartMarker(it));
}

/** Freeze live "Thinking for Xs" clocks on all reasoning items — called when
 *  any later block starts (text / code / a new reasoning block) or the turn
 *  ends. The block's elapsed (now − reasoningStartedAt) is stamped into
 *  `reasoningElapsedMs` and the live anchor dropped, so the chip immediately
 *  shows a frozen "Thought for Xs" instead of falling back to a bare
 *  "Thinking" until the snapshot commits the authoritative backend value.
 *  Only touches genuine live clocks (`reasoningStartedAt` set, no committed
 *  `reasoning_ms` yet); committed items keep their backend duration. Each
 *  reasoning block is timed independently — model-agnostic, no assumption
 *  that thinking precedes all text. */
export function freezeReasoningClocks(items: BackendTimelineItem[]): BackendTimelineItem[] {
  return items.map((it) => {
    if (it.kind === "agent_reasoning" && it.reasoningStartedAt != null && it.reasoning_ms == null) {
      const { reasoningStartedAt, ...rest } = it;
      return { ...rest, reasoningElapsedMs: Date.now() - reasoningStartedAt };
    }
    return it;
  });
}

/** Clear live "writing code for Xs" clocks from all code items —
 *  called when the model transitions from code writing to execution.
 *  Only touches items whose `codeStartedAt` is set (genuine live clocks). */
export function clearCodeClocks(items: BackendTimelineItem[]): BackendTimelineItem[] {
  return items.map((it) => {
    if (it.kind === "agent_code" && it.codeStartedAt != null) {
      const { codeStartedAt: _, ...rest } = it;
      return { ...rest, codeElapsedMs: Date.now() - it.codeStartedAt };
    }
    return it;
  });
}

/** Fold one SystemEvent into the BackendTimelineItem list. Returns a new list (does not mutate the input). */
export function applySystemEvent(
  items: BackendTimelineItem[],
  ev: SystemEvent,
): BackendTimelineItem[] {
  const now = new Date().toISOString();
  switch (ev.role) {
    case "chat_start":
      // Chat content begins → reasoning + code phases are over. Freeze the
      // live "Thinking for Xs" clock (stamp its elapsed) and clear the
      // "writing code for Xs" clock from earlier blocks, so their chips show
      // the frozen duration rather than clocks that keep ticking through
      // later phases.
      return freezeReasoningClocks(
        clearCodeClocks(createStartItem(items, ev.item_id, "agent_chat")),
      );
    case "chat_delta":
      return appendDeltaById(items, ev.item_id, "agent_chat", ev.content);
    case "code_start":
      // Code content begins → reasoning phase is over (same as chat_start),
      // and any previous code block's clock (multi-code turn) is stale.
      // Freeze/clear FIRST, then create the new block, so the new block's own
      // live clock anchor survives — creating first would hand the fresh item
      // to clearCodeClocks, which would immediately strip its codeStartedAt
      // and stamp codeElapsedMs=0 (the bug that kept "Writing code for Xs"
      // from ever appearing during streaming).
      return createStartItem(
        freezeReasoningClocks(clearCodeClocks(items)),
        ev.item_id,
        "agent_code",
      );
    case "code_delta":
      return appendDeltaById(items, ev.item_id, "agent_code", ev.content);
    case "reasoning_start":
      // A new reasoning block starts → the previous reasoning block (if its
      // clock is still live, e.g. two thinking blocks with no text between)
      // is frozen, and any code-writing clock is cleared. The new block's own
      // live clock is started by createStartItem (added after the freeze, so
      // it keeps its fresh anchor).
      return createStartItem(
        freezeReasoningClocks(clearCodeClocks(items)),
        ev.item_id,
        "agent_reasoning",
      );
    case "reasoning_delta":
      return appendDeltaById(items, ev.item_id, "agent_reasoning", ev.content);
    case "exec_start":
      // Code stream finished, subprocess began executing — create a
      // code_output placeholder immediately so the output block appears
      // before any chunk arrives. The item_id matches subsequent
      // ExecOutputChunk / ExecOutput events. Clear any exec_start
      // markers left from a previous-format session (backward compat)
      // and clear any codeStartedAt clocks (code writing → execution).
      return clearCodeClocks(
        upsertItemById(removeExecStartMarkers(items), {
          item_id: ev.item_id,
          kind: "code_output",
          source: null,
          payload: "",
          created_at: now,
          inbound_id: null,
          show_timestamp: true,
          execStartedAt: Date.now(),
        }),
      );
    case "exec_output_chunk":
      // A keepalive lets a mid-stream join that missed exec_start create the
      // live output block. Once the block exists, retain the same reference so
      // ~2Hz keepalives do not create re-render churn or append visible text.
      if (ev.keepalive === true) {
        if (items.some((it) => it.item_id === ev.item_id)) return items;
        return appendDeltaById(items, ev.item_id, "code_output", "");
      }
      // subprocess stdout/stderr streaming chunks — append to code_output
      // under the same item_id (same behavior as chat/code *_delta).
      // The final ExecOutput same-id upserts and replaces with the
      // commit version that carries an envelope header.
      return appendDeltaById(items, ev.item_id, "code_output", ev.content);
    case "exec_output":
      // exec done → clear the "executing…" marker, then upsert
      // exec_output (stable item_id `{exec_msg_idx}.0`). On reload it
      // collides with the gateway snapshot via same id and dedupes;
      // chunks already streamed-appended are replaced by the full
      // envelope version. Preserve execStartedAt from the chunk-created
      // item (if any) so the output chip continues showing the live
      // duration until the snapshot commits exec_ms.
      return (() => {
        const cleaned = removeExecStartMarkers(items);
        const existing = cleaned.find((it) => it.item_id === ev.item_id);
        return upsertItemById(cleaned, {
          item_id: ev.item_id,
          kind: "code_output",
          source: null,
          payload: ev.content,
          created_at: now,
          inbound_id: null,
          show_timestamp: true,
          ...(existing?.execStartedAt != null ? { execStartedAt: existing.execStartedAt } : {}),
        });
      })();
    case "compact_request":
      // Internal event — compact request notification is not user-visible.
      return items;
    case "compact_done":
      // Internal event — compact completion notification is not user-visible.
      // The timeline reload triggered by useTimeline still fires; only the
      // visible marker is suppressed.
      return items;
    case "error": {
      const blockedError =
        ev.blocked === true &&
        ev.error_class !== null &&
        ev.error_class !== undefined &&
        ev.reason !== null &&
        ev.reason !== undefined &&
        ev.recovery !== null &&
        ev.recovery !== undefined
          ? "Provider permanently rejected the request " +
            `(classification: ${ev.error_class}; reason: ${ev.reason}). ` +
            "The agent is blocked and automated heartbeat retries are stopped. " +
            `Recovery: ${ev.recovery} Details: ${ev.content}`
          : ev.content;
      return [
        ...items,
        {
          item_id: `_marker.${now}`,
          kind: "system_marker",
          source: null,
          payload: `error:${blockedError}`,
          created_at: now,
          inbound_id: null,
          // Rendered by the ephemeral error path, which ignores the flag; true
          // satisfies the always-present contract.
          show_timestamp: true,
        },
      ];
    }
    case "cancelled":
      // Stop button. The in-flight streaming bubble is dropped by the store's
      // processSseEvent, which owns msg_count — the signal for "uncommitted"
      // (a normal streamed bubble has `partial` unset, so it cannot be
      // identified here). The reducer stays a no-op for switch exhaustiveness.
      // Silent — no marker.
      return items;
    // The following cases are all no-ops in the timeline reducer (each
    // consumed by its own independent hook). Merged into a single fall-
    // through block so every case label becomes a type-exhaustive
    // guard. When written apart, adjacent case bodies are identical;
    // removing one would fall through to the next with the same
    // behavior, and mutation testing could not catch a missing case
    // label (split-out bodies are equivalent mutants). Merged, any
    // missing case label causes that role event to fall through the
    // switch → undefined, and downstream setState immediately blows up
    // (fail loud).
    /* eslint-disable no-fallthrough */
    case "inbound_arrived":
      // Optimistic update removed — inbound_arrived only triggers
      // turnActive via processSseEvent; no placeholder is inserted in
      // the timeline. Message content is rendered by the snapshot
      // envelope-wrap after inbound_committed → reload.
    case "inbound_committed":
      // reload handled by useTimeline
    case "token_usage":
      // consumed by the useTokenUsage hook, does not enter the timeline
    case "llm_done":
      // reload handled by useTimeline
    case "label_updated":
      // sidebar behavior — useAgentsCacheSync merges into the agents query cache.
    case "page_opened":
    case "page_closed":
      // page lifecycle consumed independently by the inspector's open-pages
      // list (useAgentPages folds it into its cache), does not enter the timeline.
    case "agent_spawned":
    case "agent_updated":
      // sidebar behavior — useAgentsCacheSync merges the snapshot into the
      // agents query cache. processSseEvent early-returns before reaching this
      // reducer, but the case labels stay for switch exhaustiveness.
    case "notice_posted":
    case "notice_resolved":
      // inbox feed — useInboxFeed invalidate-refetches on these; they never
      // enter the timeline.
    case "task_created":
    case "task_updated":
      // task board — useTasks invalidate-refetches on these; they never enter
      // the per-agent timeline.
    case "cluster_update_started":
      // root resilience provider owns the full-screen update takeover; this
      // cluster-level event never enters an agent timeline.
    case "timeline_snapshot":
      // Handled by store.processSseEvent before applySystemEvent —
      // does not go through the delta reducer; uses mergeSnapshotWithStreaming directly.
      return items;
    /* eslint-enable no-fallthrough */
  }
}

/** Merge a server snapshot into the current items (incremental design).
 *
 * Each graph node enter the agent publishes a timeline_snapshot carrying
 * ONLY the messages committed since its last snapshot (`_node_log.py`,
 * rendered from in-memory state.messages — race-free, checkpoint commits
 * are async). Full-window snapshots (process start / after compact / turn
 * end) carry the tail window; the frontend cannot and need not tell the
 * shapes apart — both merge by item_id.
 *
 * Rules:
 * 1. item_id in the snapshot → the snapshot's commit version wins
 *    (replaces a streaming partial with the full content, and carries
 *    the committed metadata: reasoning_ms / exec_ms / inbound_id);
 * 2. every prev item NOT in the snapshot is kept verbatim — committed
 *    items, scroll-loaded history, ephemeral markers (they carry "this
 *    happened" info), and 0.0 when an incremental snapshot doesn't carry
 *    it (keeping it by the generic rule avoids re-allocating the ~128KB
 *    object);
 * 3. narrow stale rule: a prev item still flagged `partial` is an
 *    uncommitted generation — normally the single future position
 *    (`msg_idx == msg_count`, LLM/exec still streaming) or tracked in
 *    `streamingIds` (so a cancel can drop it by id). A partial that is
 *    neither (error-interrupted turn, stale after compact) is dropped so
 *    it cannot linger as a ghost bubble.
 *
 * Compaction is NOT reconciled here: the store hard-resets the thread on
 * `compact_done` (see timeline-store.ts) because compaction shrinks the
 * history and keep-all would resurrect pre-compact items.
 *
 * Cancel does NOT go through this merge either: the store drops the
 * abandoned generation eagerly by id (streamingIds) on `cancelled`.
 *
 * Fallback path (PR #323 streaming corruption → non-streaming ainvoke):
 * commits the full AIMessage in one shot → the next snapshot contains
 * commit `N.0` → matching by item_id replaces the partial's content.
 */
export function mergeSnapshotWithStreaming(
  prev: BackendTimelineItem[],
  snapshot: BackendTimelineItem[],
  msg_count: number,
  streamingIds: ReadonlySet<string> = EMPTY_STREAMING_IDS,
): BackendTimelineItem[] {
  const snapshotIds = new Set(snapshot.map((it) => it.item_id));
  const clientExtras = prev.filter((it) => {
    if (snapshotIds.has(it.item_id)) return false;
    if (!it.partial) return true;
    // Rule 3: stale partial cleanup — keep only the in-flight ones.
    if (streamingIds.has(it.item_id)) return true;
    const parsed = parseItemId(it.item_id);
    if (parsed === null) return true;
    return parsed[0] === msg_count;
  });
  // Sort by item_id's msg_idx.block_idx — ephemeral markers go through the
  // parseItemId null path and land at the end, preserving event ordering.
  const merged = sortByItemId([...snapshot, ...clientExtras]);
  // Shallow compare: if merged and prev have the same length and matching
  // references at each position, return prev so Zustand setState sees the
  // same reference and short-circuits, skipping a React re-render.
  if (prev.length === merged.length) {
    let same = true;
    for (let i = 0; i < prev.length; i++) {
      if (prev[i] !== merged[i]) {
        same = false;
        break;
      }
    }
    if (same) return prev;
  }
  return merged;
}

// Default for the streamingIds parameter — a caller that has no tracking
// (pure reducer tests) simply never keeps stale partials by that path.
const EMPTY_STREAMING_IDS: ReadonlySet<string> = new Set();

// =============================================================
// One thread's fold state + per-thread reducer (from timeline-store)
// =============================================================
/** One thread's foldable timeline state — the unit that lives in the top-level
 *  store fields while the thread is active, and in a `threads` map bucket while
 *  it is parked. Token fields are intentionally absent: they are cached
 *  per-thread in React Query (`["token-usage", agentId]`) via useTokenUsage, so
 *  bucketing them here would double the per-thread token source. `foldEvent` is
 *  the pure reducer over this shape. */
export interface ThreadTimelineState {
  items: BackendTimelineItem[];
  streamingIds: Set<string>;
  streamingCode: boolean;
  turnActive: boolean;
  hasMoreOlder: boolean;
  /** How many times loadOlder has been called on this thread — drives
   * exponential growth of the older-window fetch limit. Reset to 0 on a
   * cold thread start. */
  olderFetchCount: number;
  /** Compact-reset window flag (per-thread). Set when compact_done arrives
   * for this thread (active or parked): the whole history was rewritten
   * (shrink), keep-all merging would resurrect pre-compact items, and a GET
   * fired during the window may read a lagging pre-compact checkpoint.
   * While set, GET merges are dropped and the first NON-EMPTY
   * timeline_snapshot replaces the items wholesale and clears the flag. */
  resetPending: boolean;
}
/** Fold one SSE business event into a SINGLE thread's timeline state — the pure
 *  reducer shared by the active thread (top-level fields) and every parked
 *  background thread (a `threads` bucket). Returns a new ThreadTimelineState.
 *  `token_usage` / `agent_spawned` / `agent_updated` never reach here — they are
 *  handled by `processSseEvent` before this (token writes the top-level active
 *  token fields only; spawn/update belong to the agents query cache). */
export function foldEvent(t: ThreadTimelineState, ev: SystemEvent): ThreadTimelineState {
  // timeline_snapshot: the incremental snapshot the agent publishes on each
  // graph node enter (the gateway forwards it) — only the messages committed
  // since its last snapshot (full-window on process start / after compact /
  // turn end; the two shapes merge identically). msg_count = the FULL
  // len(state.messages); mergeSnapshotWithStreaming distinguishes the still-
  // streaming future partial by msg_idx == msg_count.
  if (ev.role === "timeline_snapshot") {
    const snapItems = ev.items as unknown as BackendTimelineItem[];
    // Full-window snapshots (first publish of a process, compact shrink,
    // claim turn-end fallback) carry the system-prompt item 0.0; incremental
    // ones never do (message 0 is below the cursor). Either way the merge's
    // id-replace rule keeps a single copy — no special-casing needed.
    const snapshotIds = new Set(snapItems.map((it) => it.item_id));
    return {
      ...t,
      // Streamed ids now in the snapshot are committed → stop tracking them;
      // ids still in flight stay tracked for a later cancel.
      streamingIds: new Set([...t.streamingIds].filter((id) => !snapshotIds.has(id))),
      items: mergeSnapshotWithStreaming(t.items, snapItems, ev.msg_count, t.streamingIds),
    };
  }

  // cancelled: the kernel discards a cancelled generation (commits nothing), so
  // the still-streaming bubbles are dropped by the exact ids streamed this turn
  // (streamingIds), not a msg_count boundary that could be stale after
  // SSE-missed snapshots. code_output is never in streamingIds, so an
  // interrupted exec's partial output survives; the empty exec placeholder and
  // the old-format "executing…" marker are cleared. Silent: no marker added.
  if (ev.role === "cancelled") {
    return {
      ...t,
      turnActive: false,
      streamingCode: false,
      streamingIds: new Set(),
      items: t.items.filter(
        (it) =>
          !t.streamingIds.has(it.item_id) &&
          !(it.kind === "system_marker" && it.payload === "exec_start") &&
          !(it.kind === "code_output" && it.payload === "" && it.exec_ms == null),
      ),
    };
  }

  // turn lifecycle tracking
  const turnStart =
    ev.role === "inbound_arrived" ||
    ev.role === "chat_start" ||
    ev.role === "code_start" ||
    ev.role === "reasoning_start" ||
    ev.role === "exec_start";
  // cancelled is handled above (it flips both flags off); intentionally absent here.
  const turnEnd = ev.role === "llm_done" || ev.role === "error";

  // streaming flag
  const codeStart = ev.role === "code_start";
  const codeEnd =
    ev.role === "exec_start" ||
    ev.role === "exec_output" ||
    ev.role === "compact_done" ||
    ev.role === "error";

  // Track the ids of agent generation bubbles being streamed this turn (chat /
  // code / reasoning, by start or delta) so a cancel can drop exactly them.
  // exec_output_chunk (code_output) is deliberately not tracked — exec output
  // is not discarded on cancel.
  const isAgentStream =
    ev.role === "chat_start" ||
    ev.role === "chat_delta" ||
    ev.role === "code_start" ||
    ev.role === "code_delta" ||
    ev.role === "reasoning_start" ||
    ev.role === "reasoning_delta";
  const streamingIds = isAgentStream
    ? new Set(t.streamingIds).add(ev.item_id)
    : t.streamingIds;

  // Turn over → freeze any still-live reasoning clock and clear any code-writing
  // clock for items that never got their committed duration, so live clocks stop
  // instead of ticking on dead items. The guard keeps array identity stable when
  // there is no live clock to act on.
  const applied = applySystemEvent(t.items, ev);
  const hasLiveClock =
    turnEnd &&
    applied.some(
      (it) =>
        (it.reasoningStartedAt != null && it.reasoning_ms == null) ||
        it.codeStartedAt != null ||
        (it.execStartedAt != null && it.exec_ms == null),
    );
  const items = (() => {
    if (!hasLiveClock) return applied;
    const frozen = clearCodeClocks(freezeReasoningClocks(applied));
    // Also clear execStartedAt from code_output placeholders — if the turn
    // ends while a code_output placeholder still has its live clock (e.g.
    // llm_done after exec_start but before exec_output commits), the
    // leftover clock keeps isLiveExecution true → liveIndex peels the item
    // out of grouping → the item never folds into its turn. The clock served
    // its purpose (showed "executing…" while live); clear it so the completed
    // turn groups correctly.
    return frozen.map((it) => {
      if (it.kind === "code_output" && it.execStartedAt != null && it.exec_ms == null) {
        const { execStartedAt: _, ...rest } = it;
        return rest;
      }
      return it;
    });
  })();

  return {
    ...t,
    turnActive: turnStart ? true : turnEnd ? false : t.turnActive,
    streamingCode: codeStart ? true : codeEnd ? false : t.streamingCode,
    streamingIds,
    items,
  };
}
