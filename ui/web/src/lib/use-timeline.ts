// The selected conversation owns one HTTP tail snapshot and one live view.
// Switching drops inactive live state; durable older history stays pageable.
// Snapshot reads and history pages abort when their selection loses ownership.
// A retained window renders instantly on a switch back; the shared reconcile
// (agent-reconcile.ts) refreshes the trio with one read on every re-attach.

"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { startTransition, useCallback, useEffect, useLayoutEffect, useRef } from "react";

import { api } from "./api";
import { useAgentReconcile } from "./agent-reconcile";
import { useDisplayLimit } from "./display-limits";
import { useAgentReadRepair } from "./use-agent-read-repair";
import { inspectLiveQueryKey } from "./inspector-queries";
import { errMsg } from "./errors";
import { noteTurnStart } from "./interaction-timing";
import { useTimelineStore } from "./timeline-store";
import { CONVERSATION_RETENTION_MS } from "./switch-budget";
import { isReattachedTimelineContext, parseItemIdParts, standingHeadNoteIds } from "./timeline";
import { useCompactHistoryRetention } from "./use-compact-history-retention";

/** Baked fallback for the base number of items fetched per scroll-up — the
 * live value is display.timeline_history_page_base, read at runtime from
 * GET /api/config (useDisplayLimit; task #3696). Subsequent scroll-ups fetch
 * BASE * 2^olderFetchCount items (capped at 1000), so the window grows
 * exponentially instead of linearly — fewer scroll-ups needed. */
const OLDER_BASE_LIMIT = 50;
import type { BackendTimelineItem, SystemEvent, TimelineResponse } from "./types";
import type { ConnectionEvent } from "./useEventStream";
import { useAgentEventStream } from "./useEventStream";

function startsTurn(event: SystemEvent): boolean {
  return (
    event.role === "inbound_arrived" ||
    event.role === "chat_start" ||
    event.role === "code_start" ||
    event.role === "reasoning_start" ||
    event.role === "exec_start"
  );
}

/** SSE connection state — UI uses it to show banners ("disconnected /
 *  reconnecting"). Parse failures are signals,
 *  not health-state changes. */
export type ConnectionState = Exclude<ConnectionEvent["type"], "parse-failed">;

export interface UseTimelineResult {
  items: BackendTimelineItem[];
  /** Whether the last agent_code is streaming — used by PythonCode for the blinking cursor */
  streamingCode: boolean;
  /** SSE connection state — when agentId=null, keeps the initial value (no subscription, no banner). */
  connectionState: ConnectionState;
  /** Whether the current turn is running — derived from the SSE
   *  event stream; the composer button uses it to switch between
   *  send / stop as the selected conversation progresses. Turn boundaries:
   *    start: inbound_arrived / chat_start / code_start / reasoning_start /
   *           exec_start
   *           (chat/code/reasoning/exec)
   *    end:   llm_done / exec_output / cancelled / error / SSE closed
   *  Multi-step: briefly false between exec_output → next *_start,
   *  which matches the agent's real state (agent is idle waiting for
   *  the next step at that moment).
   *  ~100ms (network RTT) between send and inbound_arrived: the button
   *  still shows send; accept this delay in exchange for "the message
   *  renders as a single envelope-wrap" cleanness. */
  turnActive: boolean;
  /** Whether the initial snapshot is loading — true on cold cache
   *   (thread never loaded) until fetch completes. Always false on hot
   *   cache hit (data instantly available). */
  isLoading: boolean;
  /** Background refresh in flight — during stale-while-revalidate;
   *   data is already displayed at this point. */
  isRefetching: boolean;
  /** Whether older items exist before the oldest loaded one — the timeline
   *   shows a scroll-up affordance only when true. */
  hasMoreOlder: boolean;
  /** An older-window fetch is in flight (scroll-up). */
  loadingOlder: boolean;
  /** Fetch the previous window of older items and prepend them. The
   *   timeline calls this when the user scrolls near the top. No-op when
   *   already loading or no older items remain. */
  loadOlder: () => void;
}

export function useTimeline(
  agentId: number | null,
  showError: (msg: string) => void,
): UseTimelineResult {
  const queryClient = useQueryClient();

  const { isVisible } = useAgentReadRepair("timeline", agentId);
  const { requestReconcile, abortReconcile } = useAgentReconcile(agentId);
  const olderRequest = useRef<AbortController | null>(null);
  const timelineQuery = useQuery({
    queryKey: ["timeline", agentId] as const,
    queryFn: ({ signal }) => {
      if (agentId === null) throw new Error("A timeline read requires an agent");
      return api.getTimeline(agentId, { signal });
    },
    enabled: agentId !== null && isVisible,
    // A retained window is also fresh (task #3900 batch 2): switching back
    // seeds from cache and paints immediately, and the switch itself fires
    // no read — RQ auto-reads a key switch only when the target key is
    // stale. The one refresh is the re-attach reconcile
    // (agent-reconcile.ts); SSE invalidations still refetch explicitly.
    staleTime: CONVERSATION_RETENTION_MS,
    gcTime: CONVERSATION_RETENTION_MS,
    refetchOnMount: false,
  });

  useEffect(() => {
    if (!isVisible && agentId !== null) {
      olderRequest.current?.abort();
      useTimelineStore.setState({ loadingOlder: false });
    }
    return () => { olderRequest.current?.abort(); };
  }, [agentId, isVisible]);

  // -- Subscribe to timeline state from the Zustand store --
  const items = useTimelineStore((s) => s.items);
  const streamingCode = useTimelineStore((s) => s.streamingCode);
  const connectionState = useTimelineStore((s) => s.connectionState);
  const turnActive = useTimelineStore((s) => s.turnActive);
  const processSseEvent = useTimelineStore((s) => s.processSseEvent);
  const processSseEventBatch = useTimelineStore((s) => s.processSseEventBatch);
  const processConnectionEvent = useTimelineStore((s) => s.processConnectionEvent);
  const reloadSnapshot = useTimelineStore((s) => s.reloadSnapshot);
  const switchThread = useTimelineStore((s) => s.switchThread);
  const clearPartialFlags = useTimelineStore((s) => s.clearPartialFlags);
  const hasMoreOlder = useTimelineStore((s) => s.hasMoreOlder);
  const loadingOlder = useTimelineStore((s) => s.loadingOlder);
  const beginLoadOlder = useTimelineStore((s) => s.beginLoadOlder);
  const prependOlder = useTimelineStore((s) => s.prependOlder);

  // parse-failed dedupe: toast the same error message only once per
  // thread to prevent schema drift on high-frequency events (e.g.
  // code_delta) from flooding the toast slot and crowding out other
  // errors. Reset on the open event (new connection resets noisy state).
  const seenParseErrors = useRef<Set<string>>(new Set());

  const compactPending = useRef(false);

  const lastAppliedDataRef = useRef<TimelineResponse | null>(null);

  useLayoutEffect(() => {
    seenParseErrors.current.clear();
    compactPending.current = false;
    const cached = agentId === null ? null :
      queryClient.getQueryData<TimelineResponse>(["timeline", agentId]);
    switchThread(agentId, cached?.items ?? null, cached?.has_more ?? false);
    lastAppliedDataRef.current = cached ?? null;
    return () => { switchThread(null, null, false); };
  }, [agentId, queryClient, switchThread]);

  // Apply each distinct authoritative response once.
  useEffect(() => {
    if (!timelineQuery.data || agentId == null) return;
    if (timelineQuery.data === lastAppliedDataRef.current) return;
    lastAppliedDataRef.current = timelineQuery.data;

    reloadSnapshot(
      timelineQuery.data.items,
      timelineQuery.data.msg_count,
      timelineQuery.data.has_more,
    );
  }, [timelineQuery.data, agentId, reloadSnapshot]);

  // -- React Query error → toast + clear partial flags --
  useEffect(() => {
    if (timelineQuery.error) {
      showError(`Failed to load timeline: ${errMsg(timelineQuery.error)}`);
      clearPartialFlags();
    }
  }, [timelineQuery.error, showError, clearPartialFlags]);

  // -- SSE business-event handler --
  // Wrap delta-streaming events in startTransition to lower React render
  // priority — so successive code_delta / chat_delta chunk re-renders don't
  // block UI interactions (e.g. clicking stop). Lifecycle events
  // (start/committed/done/cancelled) aren't delayed — the user needs to see
  // the button flip and reload result immediately.
  const isStreamingDelta = (ev: SystemEvent): boolean =>
    ev.role === "code_delta" ||
    ev.role === "chat_delta" ||
    ev.role === "reasoning_delta" ||
    ev.role === "exec_output_chunk";

  // Compaction changes the selected read models; inactive views own no work.
  const trackCompactForInvalidation = useCallback(
    (ev: SystemEvent) => {
      if (agentId === null || ev.agent_id !== agentId) return;
      if (ev.role === "impersonation_changed" || ev.role === "inbound_arrived") {
        void queryClient.invalidateQueries({ queryKey: ["timeline", ev.agent_id] });
      } else if (ev.role === "compact_done") {
        compactPending.current = true;
        olderRequest.current?.abort();
        void queryClient.cancelQueries({ queryKey: ["timeline", agentId], exact: true });
        // Same reason the timeline query's own fetch is cancelled above: a
        // composed reconcile read started before the compact must not land
        // its pre-compact snapshot after the post-compact window.
        abortReconcile();
      } else if (
        ev.role === "timeline_snapshot" &&
        (ev.items as unknown as unknown[] | undefined)?.length &&
        compactPending.current
      ) {
        compactPending.current = false;
        void queryClient.invalidateQueries({ queryKey: ["timeline", ev.agent_id] });
        void queryClient.invalidateQueries({
          queryKey: inspectLiveQueryKey(ev.agent_id),
        });
        void queryClient.invalidateQueries({ queryKey: ["agent-inspect", ev.agent_id] });
        void queryClient.invalidateQueries({ queryKey: ["pending", ev.agent_id] });
        void queryClient.invalidateQueries({ queryKey: ["token-usage", ev.agent_id] });
      }
    },
    [agentId, queryClient, abortReconcile],
  );

  // Streaming-delta roles — rendered through startTransition so a burst of
  // chunks never blocks UI interactions (shared by the per-event and batch
  // paths; a batch containing ANY delta renders as a transition — a lifecycle
  // event in the same frame is delayed by at most one transition commit).
  const onSystemEvent = useCallback(
    (ev: SystemEvent) => {
      // token_usage is owned solely by useTokenUsage (its own processSseEvent
      // call on the same shared AgentEventStreamProvider). The timeline slice
      // must not also forward it — otherwise every token_usage event writes the
      // token state twice (R10 double-processing). It carries nothing the
      // timeline renders: applySystemEvent treats token_usage as a no-op.
      if (ev.role === "token_usage") return;
      if (startsTurn(ev)) noteTurnStart(ev.agent_id);

      if (isStreamingDelta(ev)) {
        startTransition(() => {
          processSseEvent(ev);
        });
      } else {
        processSseEvent(ev);
      }
      trackCompactForInvalidation(ev);
    },
    [processSseEvent, trackCompactForInvalidation],
  );

  // Frame-batch delivery: the provider calls this ONCE per SSE frame with
  // every event in it, and the whole frame folds in one store set() (one
  // notification + one render) instead of one set() per event. This is the
  // subscriber-level half of the streaming render fix — one active agent can
  // still produce many deltas per second, and the per-event path turned every
  // one into a render/layout pass on the home page.
  const onSystemEventBatch = useCallback(
    (events: SystemEvent[]) => {
      // token_usage belongs to useTokenUsage's own per-event subscriber.
      const filtered = events.filter((ev) => ev.role !== "token_usage");
      if (filtered.length === 0) return;
      for (const ev of filtered) {
        if (startsTurn(ev)) noteTurnStart(ev.agent_id);
      }
      const hasStreaming = filtered.some((ev) => isStreamingDelta(ev));
      const apply = () => processSseEventBatch(filtered);
      if (hasStreaming) {
        startTransition(apply);
      } else {
        apply();
      }
      for (const ev of filtered) {
        trackCompactForInvalidation(ev);
      }
    },
    [processSseEventBatch, trackCompactForInvalidation],
  );

  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      switch (ev.type) {
        case "open":
          processConnectionEvent({ type: "open" });
          seenParseErrors.current.clear();
          // Opening during an initial/ongoing read cannot trust that read
          // to cover the subscription gap; require a trailing read — the
          // shared composed reconcile joins in-flight reads, then refreshes
          // all three conversation models with one request.
          requestReconcile();
          // A compact whose post-compact snapshot was lost in the gap is now
          // covered by the reconnect reconcile — drop any pending marker so a
          // later snapshot does not double-invalidate.
          compactPending.current = false;
          return;
        case "reconnecting":
          processConnectionEvent({ type: "reconnecting" });
          return;
        case "closed":
          processConnectionEvent({ type: "closed" });
          return;
        case "parse-failed": {
          const key = String(ev.error);
          if (seenParseErrors.current.has(key)) return;
          seenParseErrors.current.add(key);
          showError(`SSE event parse failed: ${key}`);
          return;
        }
      }
    },
    [showError, processConnectionEvent, requestReconcile],
  );

  useAgentEventStream(onSystemEvent, onConnectionEvent, onSystemEventBatch);

  // Scroll-up base window: display.timeline_history_page_base, read at runtime
  // from /api/config; the baked OLDER_BASE_LIMIT holds until the read lands.
  const olderBaseLimit = useDisplayLimit("AVA_TIMELINE_HISTORY_PAGE_BASE", OLDER_BASE_LIMIT);

  // -- Scroll-up: fetch + prepend the previous window of older items --
  // Reads live store state via getState() (not the subscribed values) so
  // the callback stays stable and never fires on stale closures. The cursor
  // is the oldest item with a stable backend id (ephemeral `_marker.*` items
  // the backend doesn't know are skipped).
  // Resolves true when a fetch actually ran — the compact-history retention
  // hook (use-compact-history-retention.ts) walks N segments off this.
  const loadOlderSegment = useCallback(async (): Promise<boolean> => {
    if (agentId == null || !isVisible) return false;
    const st = useTimelineStore.getState();
    if (st.activeThreadId !== agentId || !st.hasMoreOlder || st.loadingOlder) return false;
    // Current standing context is never a cursor: the re-attached prompt, the
    // standing head notes (exec timeout / timezone / cluster memory / agent id
    // / agent memory — re-attached by the gateway beside the prompt), and
    // compact summaries. A cursor on a head note would make the gateway cross
    // straight to the older compact segment, skipping every real item between
    // the head and the tail window. A segment-prefixed historical summary is
    // the bounded continuation for a summary-only checkpoint, however: its
    // exact checkpoint id lets the server advance one segment without scanning
    // an unbounded chain in one request.
    const headNoteIds = standingHeadNoteIds(st.items);
    const isRealCursor = (it: BackendTimelineItem): boolean => {
      const parts = parseItemIdParts(it.item_id);
      if (parts === null) return false;
      if (headNoteIds.has(it.item_id)) return false;
      return (
        !isReattachedTimelineContext(it) ||
        (parts.rank > 0 && it.kind === "inbound_compact_summary")
      );
    };
    let oldest = st.items.find(isRealCursor);
    // Head-only segment fallback (task #3698): right after a compact the
    // current segment can hold nothing but the prompt, the standing head
    // notes and the compact summary. With no real item, a cursor on the
    // summary crosses to the previous segment and skips nothing — every
    // item before it is re-attached context, exactly the gateway's
    // `_window_or_cross` head rule. This is what lets compact-history
    // retention re-attach the previous session before any new activity.
    oldest ??= st.items.find((it, idx) => {
      if (it.kind !== "inbound_compact_summary") return false;
      if (parseItemIdParts(it.item_id)?.rank !== 0) return false;
      return st.items
        .slice(0, idx)
        .every((prev) => isReattachedTimelineContext(prev) || headNoteIds.has(prev.item_id));
    });
    if (oldest === undefined) return false;
    // Exponential growth: first fetch N, second 2N, third 4N, … capped at 1000
    // (the endpoint's protective le — a constant, not config).
    const limit = Math.min(olderBaseLimit * Math.pow(2, st.olderFetchCount), 1000);
    const controller = new AbortController();
    olderRequest.current = controller;
    beginLoadOlder();
    try {
      const page = await api.getTimeline(agentId, { before: oldest.item_id, limit, signal: controller.signal });
      // Agent switch mid-flight: drop the result so it can't contaminate
      // the now-active thread (switchThread already cleared loadingOlder).
      if (controller.signal.aborted || useTimelineStore.getState().activeThreadId !== agentId) return false;
      prependOlder(page.items, page.has_more);
      // Bump the counter so the next scroll-up doubles the window.
      useTimelineStore.getState().incrementOlderFetchCount();
      return true;
    } catch (e: unknown) {
      if (controller.signal.aborted) return false;
      useTimelineStore.setState({ loadingOlder: false });
      showError(`Failed to load older messages: ${errMsg(e)}`);
      return false;
    }
  }, [agentId, isVisible, beginLoadOlder, prependOlder, showError, olderBaseLimit]);
  const loadOlder = useCallback(() => {
    void loadOlderSegment();
  }, [loadOlderSegment]);

  // Compact-history retention (task #3698; user ruling 2026-09-17): after a
  // compact wholesale-replace lands on this thread, re-attach the previous
  // session(s) — the logic lives in use-compact-history-retention.ts.
  useCompactHistoryRetention({
    agentId,
    isVisible,
    loadOlderSegment,
    hasMoreOlder,
    loadingOlder,
    itemCount: items.length,
  });

  return {
    items,
    streamingCode,
    connectionState,
    turnActive,
    isLoading: timelineQuery.isLoading,
    isRefetching: timelineQuery.isRefetching,
    hasMoreOlder,
    loadingOlder,
    loadOlder,
  };
}
