// useTimeline custom hook — maintains timeline items + SSE folding +
// reload coordination.
//
// State sources:
//   (a) React Query ["timeline", agentId] — GET /api/agents/{id}/timeline
//       snapshot, with per-thread cache + stale-while-revalidate. Cache
//       hit on switching back to a previously visited thread shows
//       data instantly while a background refresh runs.
//   (b) SSE /api/system — push-based event stream (global broadcast).
//   (c) Zustand timeline store (useTimelineStore) — source of UI render,
//       maintained by (a) snapshot + (b) SSE folding + reload merging.
//
// reload triggers:
//   - agentId change → React Query supplies data under the new queryKey
//     (cache hit or fetch); switchThread additionally unparks the live-folded
//     bucket if this thread was recently visited (fresher than the snapshot).
//   - SSE timeline_snapshot → the full snapshot pushed by the gateway,
//     folded directly into the active thread (no HTTP refetch).
//
// staleTime=5min design decision (PR3): the store now folds SSE events into a
// per-thread bucket even while a thread is parked (background), so a hot
// switch-back restores live state without needing an HTTP refresh. The
// aggressive staleTime=0 (refetch on every observe) is therefore redundant;
// 5min keeps switch-back from firing a background refetch that would only
// re-apply what SSE already folded. Explicit refetch is still forced where the
// snapshot can genuinely diverge from the fold — compact_done (history rewrite)
// and reconnect (events missed during the gap) — via invalidateQueries, which
// ignores staleTime. gcTime=30min preserves the cache for cold-bucket hot hits
// (a thread evicted from the parked-threads LRU, or returning from another page).
//
// Aw-Snap memory bound: the fleet-wide timeline prefetch (usePrefetchTimelines)
// was REMOVED — it cached one full timeline (with the ~128KB system-prompt
// item plus historical items) per fleet agent for gcTime=30min, the dominant
// renderer-heap retention source (~445 agents × ~2-3 copies measured in the
// heap). This hook's ["timeline", id] query (full, WITH the prompt) fires only
// for agents actually opened, so live copies ≈ visited agents + the active
// thread, never the fleet size.

"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { startTransition, useCallback, useEffect, useLayoutEffect, useRef } from "react";

import { api } from "./api";
import { errMsg } from "./errors";
import { useTimelineStore } from "./timeline-store";
import { parseItemId } from "./timeline";

/** Base number of items fetched per scroll-up. Subsequent scroll-ups
 * fetch BASE * 2^olderFetchCount items (capped at 1000), so the window
 * grows exponentially instead of linearly — fewer scroll-ups needed. */
const OLDER_BASE_LIMIT = 50;
import type { BackendTimelineItem, SystemEvent, TimelineResponse } from "./types";
import type { ConnectionEvent } from "./useEventStream";
import { useAgentEventStream } from "./useEventStream";

/** SSE connection state — UI uses it to show banners ("disconnected /
 *  reconnecting"). Derived from ConnectionEvent.type (excluding
 *  'parse-failed' — that's a per-event signal, not a health-state
 *  change). When new health variants are added, ConnectionState
 *  follows automatically. */
export type ConnectionState = Exclude<ConnectionEvent["type"], "parse-failed">;

export interface UseTimelineResult {
  items: BackendTimelineItem[];
  /** Whether the last agent_code is streaming — used by PythonCode for the blinking cursor */
  streamingCode: boolean;
  /** SSE connection state — when agentId=null, keeps the initial value (no subscription, no banner). */
  connectionState: ConnectionState;
  /** Whether the current turn is running — derived from the SSE
   *  event stream; the composer button uses it to switch between
   *  send / stop. More timely than the agents-table status 5s poll,
   *  avoiding the race where you want to cancel after send but the
   *  button is still send. Turn boundaries:
   *    start: inbound_arrived / inbound_committed / *_start
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

  // -- React Query: timeline snapshot, cached by agentId --
  // staleTime 5min: the store's per-thread SSE folding keeps buckets fresh, so
  //   a switch-back does not need a background refetch. compact_done / reconnect
  //   still force a fresh pull via invalidateQueries (which ignores staleTime).
  // gcTime 30min: keep inactive thread cache so a cold-bucket hot-hit shows
  //   instantly on agent switch AND on returning from another page.
  const timelineQuery = useQuery({
    queryKey: ["timeline", agentId] as const,
    // eslint-disable-next-line @typescript-eslint/no-non-null-assertion -- the `enabled` gate below guarantees agentId is set before queryFn runs (standard TanStack idiom the types cannot see)
    queryFn: () => api.getTimeline(agentId!),
    enabled: agentId != null,
    staleTime: 5 * 60_000,
    gcTime: 30 * 60_000,
  });

  // -- Subscribe to timeline state from the Zustand store --
  const items = useTimelineStore((s) => s.items);
  const streamingCode = useTimelineStore((s) => s.streamingCode);
  const connectionState = useTimelineStore((s) => s.connectionState);
  const turnActive = useTimelineStore((s) => s.turnActive);
  const processSseEvent = useTimelineStore((s) => s.processSseEvent);
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

  // Agent ids whose compact_done arrived but whose post-compact snapshot has
  // not yet — the deferred invalidate trigger (see onSystemEvent). Agent id 0
  // is never here: compact_done always carries the compacting agent's id.
  const pendingCompactAgentsRef = useRef<Set<number>>(new Set());

  // The last TimelineResponse object already reflected in the active thread —
  // keyed by object identity. Its PR3 job: stop the data effect below from
  // re-folding an HTTP snapshot that switchThread already accounted for. On a
  // switch-back, switchThread unparks the live-folded bucket (fresher than the
  // React Query snapshot); this ref points at that cached response so the data
  // effect skips it and does NOT clobber the fresh bucket with the stale
  // snapshot. A real refetch (compact_done / reconnect / staleTime expiry)
  // always yields a NEW object → never skipped, so genuine refreshes still
  // fold. Object identity is immune to rapid A→B→A switches a boolean would
  // confuse (R4).
  const lastAppliedDataRef = useRef<TimelineResponse | null>(null);

  // -- On agent switch: park/unpark via switchThread; record the cached
  //    response so the data effect doesn't re-fold it over the fresh bucket --
  //    useLayoutEffect: fires synchronously before browser paint so the user
  //    never sees the old agent's items flash on the new agent's timeline.
  useLayoutEffect(() => {
    if (agentId == null) return;
    seenParseErrors.current.clear();

    // Check the React Query cache — getQueryData reads synchronously and does not trigger fetch.
    // The cache holds the full timeline (with the system-prompt item) only for
    // agents actually opened in this session — the fleet-wide prefetch was
    // removed (Aw-Snap fix): it retained one ~128KB prompt copy plus historical
    // items per fleet agent for gcTime=30min, the dominant renderer-heap
    // source. A never-opened agent seeds cold and the on-demand query lands
    // within a fetch.
    const cached = queryClient.getQueryData<TimelineResponse>(["timeline", agentId]);
    // Atomic switch: park the outgoing thread, unpark this one (parked bucket
    // wins; else seed from `cached`; else cold). Record the cached object so the
    // data effect below skips re-folding it in the same commit.
    switchThread(agentId, cached ? cached.items : null, cached ? cached.has_more : false);
    lastAppliedDataRef.current = cached ?? null;
    // Reconcile any thread we switch back to that has a cached snapshot — even
    // one with a live SSE-folded bucket. The bucket is NOT a freshness guarantee:
    // an event missed during a socket gap leaves it silently stale with no
    // signal, and with staleTime=5min neither the cache nor the fold-skip guard
    // would ever repair it. So force a background refetch whenever there is
    // cached data; the reload merge returns the SAME reference when content is
    // unchanged (a fresh bucket → zero render cost), so a redundant refetch is
    // cheap and closes the stale-bucket drift window. A cold thread with NO cache
    // is skipped: useQuery already fetches it on mount, so invalidating would
    // double-fetch.
    if (cached) {
      void queryClient.invalidateQueries({ queryKey: ["timeline", agentId] });
    }
  }, [agentId, queryClient, switchThread]);

  // -- React Query data change → feed into Zustand reloadSnapshot --
  // Skip re-folding a response the switch effect already accounted for
  // (identified by object reference) — re-applying a cached snapshot over the
  // unparked live bucket would drop SSE-folded messages. Any real refetch
  // produces a new TimelineResponse, so background refreshes still flow through
  // reloadSnapshot; only the exact object switchThread just recorded is skipped.
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
  const onSystemEvent = useCallback(
    (ev: SystemEvent) => {
      // token_usage is owned solely by useTokenUsage (its own processSseEvent
      // call on the same shared AgentEventStreamProvider). The timeline slice
      // must not also forward it — otherwise every token_usage event writes the
      // token state twice (R10 double-processing). It carries nothing the
      // timeline renders: applySystemEvent treats token_usage as a no-op.
      if (ev.role === "token_usage") return;

      // Wrap delta-streaming events in startTransition to lower
      // React render priority — so successive code_delta / chat_delta
      // chunk re-renders don't block UI interactions (e.g. clicking
      // stop). Lifecycle events (start/committed/done/cancelled)
      // aren't delayed — the user needs to see the button flip and
      // reload result immediately.
      const isStreamingDelta =
        ev.role === "code_delta" ||
        ev.role === "chat_delta" ||
        ev.role === "reasoning_delta" ||
        ev.role === "exec_output_chunk";

      if (isStreamingDelta) {
        startTransition(() => {
          processSseEvent(ev);
        });
      } else {
        processSseEvent(ev);
      }

      // The agent publishes a timeline_snapshot on each graph node enter,
      // so the frontend no longer needs to refetch. LLMDone and
      // InboundCommitted also no longer trigger refetch — their
      // responsibility is replaced by the agent-published timeline_snapshot.
      //
      // compact_done is the exception: compaction replaces the whole history
      // (the old messages are removed, not appended to), and the snapshot
      // merge is built for growth, not shrinkage. The cache must reconcile to
      // the authoritative post-compact state — but an invalidate fired HERE
      // reads the checkpoint BEFORE the compact commits (the commit lands a
      // beat later, on remote machines seconds later) and would cache the
      // lagging pre-compact snapshot; a later switch-back then seeds it and
      // keep-all merging resurrects the old bubbles. So defer the invalidate
      // to the first NON-EMPTY post-compact timeline_snapshot for that agent:
      // by then the checkpoint is committed (the snapshot is emitted from the
      // in-memory state after the compacting super-steps), the refetch
      // returns the new history, and the cache + switch-back stay clean. The
      // store's reset window drops the refetch's merge while it is open, so
      // the visible thread is unaffected either way.
      if (ev.role === "compact_done") {
        pendingCompactAgentsRef.current.add(ev.agent_id);
      } else if (
        ev.role === "timeline_snapshot" &&
        (ev.items as unknown as unknown[] | undefined)?.length &&
        pendingCompactAgentsRef.current.delete(ev.agent_id)
      ) {
        void queryClient.invalidateQueries({ queryKey: ["timeline", ev.agent_id] });
      }
    },
    [processSseEvent, queryClient],
  );

  const onConnectionEvent = useCallback(
    (ev: ConnectionEvent) => {
      switch (ev.type) {
        case "open":
          processConnectionEvent({ type: "open" });
          seenParseErrors.current.clear();
          // Reconnect fallback: any events pushed while the stream was down
          // (a compact, new turns) were missed. Refetch the snapshot so the
          // timeline catches up to whatever happened during the gap. Harmless
          // on the initial open — the query is already loading then.
          if (agentId != null) {
            void queryClient.invalidateQueries({ queryKey: ["timeline", agentId] });
          }
          // A compact whose post-compact snapshot was lost in the gap is now
          // covered by the reconnect refetch — drop any pending marker so a
          // later snapshot does not double-invalidate.
          pendingCompactAgentsRef.current.clear();
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
    [showError, processConnectionEvent, queryClient, agentId],
  );

  useAgentEventStream(onSystemEvent, onConnectionEvent);

  // -- Scroll-up: fetch + prepend the previous window of older items --
  // Reads live store state via getState() (not the subscribed values) so
  // the callback stays stable and never fires on stale closures. The cursor
  // is the oldest item with a stable backend id (ephemeral `_marker.*` items
  // the backend doesn't know are skipped).
  const loadOlder = useCallback(() => {
    if (agentId == null) return;
    const st = useTimelineStore.getState();
    if (!st.hasMoreOlder || st.loadingOlder) return;
    // The cursor is the oldest item with a stable backend id (ephemeral
    // `_marker.*` items the backend doesn't know are skipped). The
    // system-prompt item 0.0 is excluded: GET /timeline re-attaches it at the
    // window head (#570), so it is the oldest parseable item, but the backend
    // `before` paging treats it as index 0 → an empty window + has_more=false
    // that permanently kills scroll-up history loading (regression #579-P1).
    const oldest = st.items.find(
      (it) => it.item_id !== "0.0" && parseItemId(it.item_id) !== null,
    );
    if (!oldest) return;
    // Exponential growth: first fetch N, second 2N, third 4N, … capped at 1000.
    const limit = Math.min(
      OLDER_BASE_LIMIT * Math.pow(2, st.olderFetchCount),
      1000,
    );
    beginLoadOlder();
    void api
      .getTimeline(agentId, { before: oldest.item_id, limit })
      .then((page: TimelineResponse) => {
        // Agent switch mid-flight: drop the result so it can't contaminate
        // the now-active thread (switchThread already cleared loadingOlder).
        if (useTimelineStore.getState().activeThreadId !== agentId) return;
        prependOlder(page.items, page.has_more);
        // Bump the counter so the next scroll-up doubles the window.
        useTimelineStore.getState().incrementOlderFetchCount();
      })
      .catch((e: unknown) => {
        useTimelineStore.setState({ loadingOlder: false });
        showError(`Failed to load older messages: ${errMsg(e)}`);
      });
  }, [agentId, beginLoadOlder, prependOlder, showError]);

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
