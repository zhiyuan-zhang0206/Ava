// De-quarantined 2026-08-24. The historical CI flake asserted a Zustand store
// notification count for a frame batch; React batching makes that an
// implementation detail. The test now asserts the final timeline state.
//
// useTimeline hook integration tests — React Testing Library + happy-dom.
//
// Complements timeline-scenarios.test.ts:
//   - timeline-scenarios: simulator runs the pure reducer + merge
//     chain, no React.
//   - here:                renderHook actually runs React state (useState /
//                          useEffect / useRef) and covers internal hook
//                          wiring (reload trigger timing, ref resets,
//                          agentId-switch effect ordering, cancelled flag
//                          races, etc.).
//
// Mock strategy:
//   - api.getTimeline: vi.mocked controls return value / delay.
//   - useEventStream: mocked as a helper that exposes onEvent so the
//     test can manually trigger SSE events (the real hook builds an
//     EventSource over the network; tests don't need that).

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { noteTurnStart } from "./interaction-timing";
import type {
  BackendTimelineItem,
  ConversationSnapshotResponse,
  SystemEvent,
  TimelineResponse,
} from "./types";
import { parseItemId } from "./timeline";
import { useTimelineStore } from "./timeline-store";
import { useTimeline } from "./use-timeline";
import type { ConnectionEvent } from "./useEventStream";
import { SETTINGS_QUERY_KEY } from "./use-user-settings";

// -- mock layer ────────────────────────────────────────────────────────────

// Spy on React.startTransition — useTimeline wraps streaming-delta
// events (code/chat/reasoning/exec_output) in startTransition to lower
// priority; non-streaming events dispatch directly. This is the only
// externally-observable branch difference — both paths end up calling
// processSseEvent(ev) with the same arg, so asserting store state alone
// can not kill the L167-172 mutant cluster. Spying on call counts is
// what pins down the isStreamingDelta boolean construction and the
// if-split mutants.
const startTransitionSpy = vi.fn((cb: () => void) => cb());
vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof React>();
  return {
    ...actual,
    startTransition: (cb: () => void) => startTransitionSpy(cb),
  };
});

vi.mock("./api", () => ({
  api: {
    getTimeline: vi.fn(),
    getConversationSnapshot: vi.fn(),
    getSettings: vi.fn(),
    // useDisplayLimit reads the display config domain; an empty field
    // list keeps every baked fallback (limit assertions below).
    getConfig: vi.fn().mockResolvedValue({ fields: [] }),
  },
}));

vi.mock("./interaction-timing", () => ({ noteTurnStart: vi.fn() }));

// useAgentEventStream mock: tests invoke events directly via
// currentEventHandler; currentConnectionHandler simulates SSE connection /
// parse events (open / closed / parse-failed). useTimeline subscribes to the
// per-active-agent stream, so we stub useAgentEventStream (not useEventStream).
let currentEventHandler: ((ev: SystemEvent) => void) | null = null;
let currentConnectionHandler: ((ev: ConnectionEvent) => void) | null = null;
let currentBatchHandler: ((evs: SystemEvent[]) => void) | null = null;
vi.mock("./useEventStream", () => ({
  // Both Providers pass children through — when useTimeline calls
  // useAgentEventStream, we stub the handlers into module-level vars
  // and skip the real Context.
  EventStreamProvider: ({ children }: { children: React.ReactNode }) => children,
  AgentEventStreamProvider: ({ children }: { children: React.ReactNode }) => children,
  useAgentEventStream: (
    onEvent: (ev: SystemEvent) => void,
    onConnectionEvent: (ev: ConnectionEvent) => void,
    onEventBatch?: (evs: SystemEvent[]) => void,
  ) => {
    currentEventHandler = onEvent;
    currentConnectionHandler = onConnectionEvent;
    currentBatchHandler = onEventBatch ?? null;
  },
}));

function pushEvent(ev: SystemEvent): void {
  if (!currentEventHandler) {
    throw new Error("useTimeline not mounted yet, no event handler");
  }
  act(() => {
    currentEventHandler!(ev);
  });
}

function pushBatch(evs: SystemEvent[]): void {
  if (!currentBatchHandler) {
    throw new Error("useTimeline did not pass a batch handler");
  }
  act(() => {
    currentBatchHandler!(evs);
  });
}

function pushConnectionEvent(ev: ConnectionEvent): void {
  if (!currentConnectionHandler) {
    throw new Error("useTimeline did not pass onConnectionEvent");
  }
  act(() => {
    currentConnectionHandler!(ev);
  });
}

// Pristine store actions, captured before any test mutates them. Several tests
// swap store actions for spies via useTimelineStore.setState({ ... }) and never restore
// them (setState overwrites are not vi spies, so restoreAllMocks can't undo
// them). Restoring these in beforeEach keeps a leftover spy from one test out of
// the next — otherwise switchThread's stable-reference contract is broken across
// tests and its effect re-fires spuriously.
const REAL_SWITCH_THREAD = useTimelineStore.getState().switchThread;
const REAL_RELOAD_SNAPSHOT = useTimelineStore.getState().reloadSnapshot;

// -- helpers ───────────────────────────────────────────────────────────────


let _id_counter_ut = 0;
function snapshotItem(overrides: Partial<BackendTimelineItem>): BackendTimelineItem {
  return {
    kind: "agent_chat",
    source: null,
    payload: "",
    created_at: "2026-01-01T00:00:00Z",
    item_id: `ut.${++_id_counter_ut}`,
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

// Wrap items into the GET /timeline response shape. msg_count is computed the
// same way the old inferMsgCount did (max parseable msg_idx + 1; the ut.N test
// ids are unparseable → 0), so reloadSnapshot sees identical (items, msg_count)
// and these tests keep their original semantics.
function postWindow(): BackendTimelineItem[] {
  return [
    snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
    snapshotItem({ item_id: "1.0", kind: "inbound_compact_summary", payload: "SUMMARY" }),
    snapshotItem({ item_id: "2.0", kind: "agent_chat", payload: "post-compact" }),
  ];
}

function tlResp(items: BackendTimelineItem[] = [], has_more = false): TimelineResponse {
  let max = -1;
  for (const it of items) {
    const parsed = parseItemId(it.item_id);
    if (parsed && parsed[0] > max) max = parsed[0];
  }
  return { items, msg_count: max + 1, has_more };
}

// The composed switch-refresh payload (agent-reconcile.ts) around a timeline
// window; token/pending sections stay empty where a test doesn't read them.
function composedSnapshot(timeline: TimelineResponse): ConversationSnapshotResponse {
  return {
    timeline,
    token_usage: { input_tokens: 0, output_tokens: 0, reasoning_tokens: 0,
      max_input_tokens: 0, soft_compact_tokens: 0, hard_compact_tokens: 0 },
    pending: [],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  currentEventHandler = null;
  currentConnectionHandler = null;
  currentBatchHandler = null;
  // Default getTimeline returns empty; each test resets as needed
  vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));
  vi.mocked(api.getConversationSnapshot).mockReset().mockResolvedValue(composedSnapshot(tlResp([])));
  vi.mocked(api.getSettings).mockResolvedValue({ settings: [] });
  // Restore any store actions a prior test swapped for spies, so switchThread /
  // reloadSnapshot are the real (stable) references again before this test runs.
  useTimelineStore.setState({ switchThread: REAL_SWITCH_THREAD, reloadSnapshot: REAL_RELOAD_SNAPSHOT });
  // Reset Zustand store to prevent cross-test contamination — the
  // store is a module-level singleton; previous tests' items /
  // connectionState must not leak into the next test.
  useTimelineStore.getState().switchThread(0, null, false);
  useTimelineStore.setState({ compactReplaceSeq: 0, compactReplaceAgent: null });
});

afterEach(() => {
  // Explicitly unmount the previous test's renderHook tree;
  // otherwise the prior hook instance remains mounted and, on store
  // re-renders, replaces currentConnectionHandler with a closure over
  // the old showError. The next test's showError mock then fails to
  // receive calls (the historical root cause of the .skip on the
  // parse-failed dedupe test).
  cleanup();
  currentEventHandler = null;
  currentConnectionHandler = null;
  currentBatchHandler = null;
});


// -- React Query wrapper ────────────────────────────────────────────────────

let queryClient: QueryClient;

beforeEach(() => {
  // Build a fresh QueryClient per test to avoid cross-test cache
  // contamination. retry: false so failing queries do not auto-retry
  // (error cases in tests need precise control).
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
});

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(QueryClientProvider, { client: queryClient }, children);
}

// -- tests ─────────────────────────────────────────────────────────────────




describe("useTimeline mount + initial fetch", () => {
  it("on mount, immediately fetches GET /timeline", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp([
      snapshotItem({ kind: "agent_chat", payload: "hi" }),
    ]));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });

    await waitFor(() => {
      expect(result.current.items).toHaveLength(1);
    });
    expect(api.getTimeline).toHaveBeenCalledWith(42, expect.objectContaining({ signal: expect.any(AbortSignal) as AbortSignal }));
    expect(result.current.items[0].payload).toBe("hi");
    expect(showError).not.toHaveBeenCalled();
  });

  it("hasMoreOlder reflects the page; loadOlder fetches before=oldest id and prepends", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(
      tlResp(
        [
          snapshotItem({ item_id: "5.0", kind: "inbound_chat", payload: "recent" }),
          snapshotItem({ item_id: "6.0", payload: "newest" }),
        ],
        true,
      ),
    );
    vi.mocked(api.getTimeline).mockResolvedValueOnce(
      tlResp([snapshotItem({ item_id: "3.0", kind: "inbound_chat", payload: "older" })], false),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    expect(result.current.hasMoreOlder).toBe(true);

    act(() => result.current.loadOlder());

    await waitFor(() => expect(result.current.items).toHaveLength(3));
    expect(api.getTimeline).toHaveBeenLastCalledWith(42, { before: "5.0", limit: 50, signal: expect.any(AbortSignal) as AbortSignal });
    expect(result.current.items.map((i) => i.item_id)).toEqual(["3.0", "5.0", "6.0"]);
    expect(result.current.hasMoreOlder).toBe(false);
    expect(showError).not.toHaveBeenCalled();
  });

  it("loadOlder skips all re-attached context and keeps paging from the oldest real item", async () => {
    // GET /timeline re-attaches the prompt and compact summary at every
    // window head. Neither is a valid cursor for the historical tail.
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(
      tlResp(
        [
          snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          snapshotItem({
            item_id: "6.0",
            kind: "inbound_compact_summary",
            payload: "SUMMARY",
          }),
          snapshotItem({ item_id: "960.1", kind: "inbound_chat", payload: "recent" }),
        ],
        true,
      ),
    );
    vi.mocked(api.getTimeline).mockResolvedValueOnce(
      tlResp(
        [
          snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          snapshotItem({
            item_id: "6.0",
            kind: "inbound_compact_summary",
            payload: "SUMMARY",
          }),
          snapshotItem({ item_id: "915.1", kind: "agent_chat", payload: "older" }),
        ],
        true,
      ),
    );
    vi.mocked(api.getTimeline).mockResolvedValueOnce(
      tlResp(
        [
          snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          snapshotItem({
            item_id: "6.0",
            kind: "inbound_compact_summary",
            payload: "SUMMARY",
          }),
          snapshotItem({ item_id: "800.1", kind: "agent_chat", payload: "oldest" }),
        ],
        false,
      ),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(3));
    expect(result.current.hasMoreOlder).toBe(true);
    vi.mocked(api.getTimeline).mockClear();

    act(() => result.current.loadOlder());

    await waitFor(() => expect(result.current.items).toHaveLength(4));
    expect(api.getTimeline).toHaveBeenNthCalledWith(1, 42, {
      before: "960.1",
      limit: 50,
      signal: expect.any(AbortSignal) as AbortSignal,
    });
    expect(result.current.items.map((i) => i.item_id)).toEqual([
      "0.0",
      "6.0",
      "915.1",
      "960.1",
    ]);
    expect(result.current.hasMoreOlder).toBe(true);

    act(() => result.current.loadOlder());

    await waitFor(() => expect(result.current.items).toHaveLength(5));
    expect(api.getTimeline).toHaveBeenNthCalledWith(2, 42, {
      before: "915.1",
      limit: 100,
      signal: expect.any(AbortSignal) as AbortSignal,
    });
    expect(result.current.items.map((i) => i.item_id)).toEqual([
      "0.0",
      "6.0",
      "800.1",
      "915.1",
      "960.1",
    ]);
    expect(result.current.hasMoreOlder).toBe(false);
    expect(showError).not.toHaveBeenCalled();
  });

  it("loadOlder skips the re-attached standing head notes as cursors", async () => {
    // The gateway re-attaches the standing head notes (exec timeout / timezone
    // / cluster memory / agent id / agent memory) right after the prompt. A
    // cursor on one of them would make the gateway cross straight to the
    // older compact segment and skip the current segment's real items between
    // the head and the tail window — paging must start at the oldest REAL item.
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(
      tlResp(
        [
          snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          snapshotItem({
            item_id: "1.0",
            kind: "system_marker",
            source: "exec_timeout",
            payload: "timeout",
          }),
          snapshotItem({
            item_id: "2.0",
            kind: "system_marker",
            source: "timezone",
            payload: "tz",
          }),
          snapshotItem({
            item_id: "3.0",
            kind: "system_marker",
            source: "memory",
            payload: "cluster memory",
          }),
          snapshotItem({
            item_id: "4.0",
            kind: "system_marker",
            source: "agent_id",
            payload: "id",
          }),
          snapshotItem({
            item_id: "5.0",
            kind: "system_marker",
            source: "agent_memory",
            payload: "agent memory",
          }),
          snapshotItem({
            item_id: "6.0",
            kind: "inbound_compact_summary",
            payload: "SUMMARY",
          }),
          snapshotItem({ item_id: "960.1", kind: "inbound_chat", payload: "recent" }),
        ],
        true,
      ),
    );
    vi.mocked(api.getTimeline).mockResolvedValueOnce(
      tlResp(
        [
          snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          snapshotItem({ item_id: "915.1", kind: "agent_chat", payload: "older" }),
        ],
        false,
      ),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(8));
    expect(result.current.hasMoreOlder).toBe(true);
    vi.mocked(api.getTimeline).mockClear();

    act(() => result.current.loadOlder());

    // The cursor is the oldest real item — never 1.0..5.0 (head notes) or 6.0.
    await waitFor(() => expect(result.current.items).toHaveLength(9));
    expect(api.getTimeline).toHaveBeenCalledTimes(1);
    expect(api.getTimeline).toHaveBeenLastCalledWith(42, {
      before: "960.1",
      limit: 50,
      signal: expect.any(AbortSignal) as AbortSignal,
    });
    expect(result.current.items.map((i) => i.item_id)).toEqual([
      "0.0",
      "1.0",
      "2.0",
      "3.0",
      "4.0",
      "5.0",
      "6.0",
      "915.1",
      "960.1",
    ]);
    expect(showError).not.toHaveBeenCalled();
  });

  it("loadOlder is a no-op when hasMoreOlder is false", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(
      tlResp([snapshotItem({ item_id: "1.0", kind: "inbound_chat", payload: "only" })], false),
    );
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    expect(result.current.hasMoreOlder).toBe(false);

    vi.mocked(api.getTimeline).mockClear();
    act(() => result.current.loadOlder());
    expect(api.getTimeline).not.toHaveBeenCalled();
  });

  it("loadOlder uses the complete historical item id as its cursor", async () => {
    const showError = vi.fn();
    const historicalId = "s2.1f0b9b12-0000-6000-8000-000000000000.14.1";
    vi.mocked(api.getTimeline)
      .mockResolvedValueOnce(
        tlResp(
          [snapshotItem({ item_id: historicalId, kind: "agent_chat", payload: "old" })],
          true,
        ),
      )
      .mockResolvedValueOnce(tlResp([], false));
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    vi.mocked(api.getTimeline).mockClear();

    act(() => result.current.loadOlder());

    await waitFor(() => expect(result.current.hasMoreOlder).toBe(false));
    expect(api.getTimeline).toHaveBeenCalledWith(42, {
      before: historicalId,
      limit: 50,
      signal: expect.any(AbortSignal) as AbortSignal,
    });
  });

  it("uses a historical compact summary as a bounded continuation cursor", async () => {
    const showError = vi.fn();
    const summaryId = "s1.1f0b9b12-0000-6000-8000-000000000000.0.0";
    vi.mocked(api.getTimeline)
      .mockResolvedValueOnce(
        tlResp(
          [
            snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
            snapshotItem({
              item_id: "1.0",
              kind: "inbound_compact_summary",
              payload: "CURRENT SUMMARY",
            }),
            snapshotItem({ item_id: "2.0", kind: "agent_chat", payload: "current" }),
            snapshotItem({
              item_id: summaryId,
              kind: "inbound_compact_summary",
              payload: "HISTORICAL SUMMARY ONLY",
            }),
          ],
          true,
        ),
      )
      .mockResolvedValueOnce(tlResp([], false));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.hasMoreOlder).toBe(true));
    vi.mocked(api.getTimeline).mockClear();

    act(() => result.current.loadOlder());

    await waitFor(() => expect(result.current.hasMoreOlder).toBe(false));
    expect(api.getTimeline).toHaveBeenCalledWith(42, {
      before: summaryId,
      limit: 50,
      signal: expect.any(AbortSignal) as AbortSignal,
    });
  });



  it("getTimeline failure calls showError; items unchanged", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockRejectedValue(new Error("boom"));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });

    await waitFor(() => {
      expect(showError).toHaveBeenCalled();
    });
    expect(showError.mock.calls[0][0]).toContain("Failed to load timeline");
    expect(result.current.items).toEqual([]);
  });
});

describe("useTimeline agentId switch", () => {
  it("agentId changes → items cleared immediately → fetches new snapshot", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline)
      .mockResolvedValueOnce(tlResp([snapshotItem({ payload: "thread-A" })]))
      .mockResolvedValueOnce(tlResp([snapshotItem({ payload: "thread-B" })]));

    const { result, rerender, unmount } = renderHook(
      ({ tid }: { tid: number | null }) => useTimeline(tid, showError),
      { initialProps: { tid: 1 }, wrapper },
    );

    await waitFor(() => expect(result.current.items[0]?.payload).toBe("thread-A"));

    rerender({ tid: 2 });

    await waitFor(() => expect(result.current.items[0]?.payload).toBe("thread-B"));
    expect(api.getTimeline).toHaveBeenLastCalledWith(2, expect.objectContaining({ signal: expect.any(AbortSignal) as AbortSignal }));
    unmount();
  });

  it("refetch within the same agent (a NEW response object) still calls reloadSnapshot to update items", async () => {
    // The data effect skips only the exact TimelineResponse object switchThread
    // already installed (by reference). A background refetch always yields a NEW
    // object, so the identity check must NOT match and reloadSnapshot must fold
    // the fresh data. If the skip were object-blind (e.g. a sticky boolean),
    // background-refetched data would never reach the store.
    const showError = vi.fn();
    vi.mocked(api.getTimeline)
      .mockResolvedValueOnce(tlResp([snapshotItem({ payload: "v1" })]))
      .mockResolvedValueOnce(tlResp([snapshotItem({ payload: "v2" })]));

    const { result, unmount } = renderHook(() => useTimeline(42, showError), { wrapper });

    // 1. Cold mount → fetch v1 → data effect folds v1 (lastAppliedDataRef = v1 obj).
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("v1"));
    expect(api.getTimeline).toHaveBeenCalledTimes(1);

    // 2. Trigger refetch (simulates inbound_committed → invalidate)
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: ["timeline", 42] });
    });

    // 3. v2 is a new object ≠ lastAppliedDataRef → data effect folds it → items become v2.
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("v2"));
    expect(api.getTimeline).toHaveBeenCalledTimes(2);

    unmount();
  });



  it("on agent switch, thread guard prevents wrong-thread events from contaminating the new thread", async () => {
    // Under the new hard-reset semantics, a partial item not in
    // snapshot is cleared — the hook's internal reloadSnapshot runs
    // again after mount and clears the just-pushed partial. So the
    // original "intermediate step verifies partial enters items"
    // approach is no longer stable.
    //
    // The core intent of this test is the thread guard: after a
    // rerender that switches threads, we receive thread-B's snapshot
    // and are not contaminated by leftover thread-A SSE. Assert the final state directly.
    const showError = vi.fn();
    vi.mocked(api.getTimeline)
      .mockResolvedValueOnce(tlResp([]))
      .mockResolvedValueOnce(tlResp([snapshotItem({ payload: "thread-B fresh" })]));

    const { result, rerender, unmount } = renderHook(
      ({ tid }: { tid: number | null }) => useTimeline(tid, showError),
      { initialProps: { tid: 1 }, wrapper },
    );

    await waitFor(() => expect(result.current.items).toEqual([]));
    // Push an event for thread A (even though it will be cleared; we verify the thread guard prevents leakage)
    pushEvent({ role: "chat_start", item_id: "5.0", agent_id: 1 });

    rerender({ tid: 2 });

    await waitFor(() =>
      expect(result.current.items[0]?.payload).toBe("thread-B fresh"),
    );
    // After switching to thread B, thread A's content is not in items (thread guard + hard reset)
    unmount();
    expect(result.current.items.find((i) => i.item_id === "5.0")).toBeUndefined();
  });



  it("activation seeds a retained snapshot; the reattach reconcile refreshes it", async () => {
    const showError = vi.fn();
    // Seed a retained snapshot that renders while the reattach reconcile runs.
    queryClient.setQueryData<TimelineResponse>(
      ["timeline", 7],
      tlResp([snapshotItem({ item_id: "1.0", payload: "stale cached" })]),
    );
    // The composed reconcile returns the current authoritative tail.
    vi.mocked(api.getConversationSnapshot).mockReset().mockResolvedValue(
      composedSnapshot(tlResp([
        snapshotItem({ item_id: "1.0", payload: "fresh" }),
        snapshotItem({ item_id: "2.0", payload: "happened while away" }),
      ])),
    );

    const { result, unmount } = renderHook(() => useTimeline(7, showError), { wrapper });

    // With cached data the mount issues no read of its own (task #3900
    // batch 2) — the retained window paints immediately.
    await waitFor(() =>
      expect(result.current.items.map((i) => i.payload)).toEqual(["stale cached"]),
    );
    expect(api.getTimeline).not.toHaveBeenCalled();

    // The re-attach reconcile refreshes it.
    pushConnectionEvent({ type: "open" });
    await waitFor(() =>
      expect(result.current.items.map((i) => i.payload)).toEqual(["fresh", "happened while away"]),
    );
    expect(api.getConversationSnapshot).toHaveBeenCalledWith(7, expect.any(AbortSignal) as AbortSignal);
    unmount();
  });

  it("compact_done for a NON-active thread does not refetch the active thread (targets ev.agent_id)", async () => {
    // A buffered compact_done for a non-active agent must NOT refetch the active
    // thread — else an inactive thread's compaction
    // spuriously refetches whatever you're viewing.
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledTimes(1)); // mount fetch

    pushEvent({ role: "compact_done", agent_id: 99 }); // a different thread compacts

    // The active thread (42) is NOT refetched — only its mount fetch happened.
    await new Promise((r) => setTimeout(r, 0));
    expect(api.getTimeline).toHaveBeenCalledTimes(1);
  });

  it("compact_done defers the refetch until a nonempty timeline snapshot arrives", async () => {
    // This preserves the existing refresh trigger. A nonempty live snapshot
    // does not prove checkpoint commit: the wire has no shared durable revision.
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledTimes(1)); // mount fetch

    // compact_done for the ACTIVE thread: no immediate refetch.
    pushEvent({ role: "compact_done", agent_id: 42 });
    await new Promise((r) => setTimeout(r, 0));
    expect(api.getTimeline).toHaveBeenCalledTimes(1);

    // An EMPTY snapshot (wiped-but-not-yet-rebuilt history) does not trigger
    // the refetch either.
    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 0, items: [] });
    await new Promise((r) => setTimeout(r, 0));
    expect(api.getTimeline).toHaveBeenCalledTimes(1);

    // The first NON-EMPTY post-compact snapshot triggers the refetch.
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 2,
      items: [snapshotItem({ item_id: "1.0", payload: "[summary]" })],
    });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledTimes(2));

    // A later snapshot for the same thread does not refetch again.
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 3,
      items: [snapshotItem({ item_id: "1.0", payload: "[summary]" })],
    });
    await new Promise((r) => setTimeout(r, 0));
    expect(api.getTimeline).toHaveBeenCalledTimes(2);
  });

  it("after a compact reset, re-attaches the previous session (default retention = 1)", async () => {
    // Task #3698 (user ruling 2026-09-17): a compact must not clear the view —
    // the just-compacted session stays visible above the new summary. The hook
    // re-attaches it automatically after the wholesale replace, through the
    // same cross-segment fetch as scroll-up.
    const showError = vi.fn();
    let phase: "pre" | "post" = "pre";
    const pageFor = (opts?: { before?: string; limit?: number }): TimelineResponse => {
      if (opts?.before === "2.0") {
        return tlResp(
          [
            snapshotItem({
              item_id: "s1.boundary.0.0",
              kind: "inbound_compact_summary",
              payload: "prev summary",
            }),
            snapshotItem({
              item_id: "s1.boundary.7.0",
              kind: "agent_chat",
              payload: "previous session",
            }),
          ],
          false,
        );
      }
      if (opts?.before !== undefined) return tlResp([], false);
      if (phase === "pre") {
        return tlResp(
          [snapshotItem({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact" })],
          true,
        );
      }
      return tlResp(postWindow(), true);
    };
    vi.mocked(api.getTimeline).mockImplementation((_id: number, opts?: { before?: string; limit?: number }) =>
      Promise.resolve(pageFor(opts)),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    expect(result.current.hasMoreOlder).toBe(true);

    phase = "post";
    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 3,
      items: postWindow(),
    });

    await waitFor(() => {
      expect(api.getTimeline).toHaveBeenCalledWith(42, { before: "2.0", limit: 50, signal: expect.any(AbortSignal) as AbortSignal });
    });
    await waitFor(() => {
      expect(result.current.items.map((i) => i.item_id)).toEqual([
        "s1.boundary.0.0",
        "s1.boundary.7.0",
        "0.0",
        "1.0",
        "2.0",
      ]);
    });
    expect(showError).not.toHaveBeenCalled();
  });

  it("head-only post-compact segment crosses from the summary cursor (no real item yet)", async () => {
    // Right after a compact the current segment can hold only the prompt and
    // the summary; with no real item, the summary itself is the safe crossing
    // cursor (every item before it is re-attached context).
    const showError = vi.fn();
    let phase: "pre" | "post" = "pre";
    const pageFor = (opts?: { before?: string; limit?: number }): TimelineResponse => {
      if (opts?.before === "1.0") {
        return tlResp(
          [
            snapshotItem({
              item_id: "s1.boundary.7.0",
              kind: "agent_chat",
              payload: "previous session",
            }),
          ],
          false,
        );
      }
      if (opts?.before !== undefined) return tlResp([], false);
      if (phase === "pre") {
        return tlResp(
          [snapshotItem({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact" })],
          true,
        );
      }
      return tlResp(
        [
          snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
          snapshotItem({ item_id: "1.0", kind: "inbound_compact_summary", payload: "SUMMARY" }),
        ],
        true,
      );
    };
    vi.mocked(api.getTimeline).mockImplementation((_id: number, opts?: { before?: string; limit?: number }) =>
      Promise.resolve(pageFor(opts)),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    phase = "post";
    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 2,
      items: [
        snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
        snapshotItem({ item_id: "1.0", kind: "inbound_compact_summary", payload: "SUMMARY" }),
      ],
    });

    await waitFor(() => {
      expect(api.getTimeline).toHaveBeenCalledWith(42, { before: "1.0", limit: 50, signal: expect.any(AbortSignal) as AbortSignal });
    });
    await waitFor(() => {
      expect(result.current.items.map((i) => i.item_id)).toEqual([
        "s1.boundary.7.0",
        "0.0",
        "1.0",
      ]);
    });
  });

  it("walks newest-first across two previous segments when display.compact_history_sessions = 2", async () => {
    // The Display row's 2/3 values walk further back: each retained session is
    // one sequential cross-segment fetch off the same scroll-up path.
    const showError = vi.fn();
    vi.mocked(api.getSettings).mockResolvedValue({
      settings: [
        { key: "display.compact_history_sessions", value: 2, updated_at: "2026-09-17T00:00:00Z" },
      ],
    });
    const fetches: (string | undefined)[] = [];
    const pageFor = (opts?: { before?: string; limit?: number }): TimelineResponse => {
      fetches.push(opts?.before);
      if (opts?.before === "2.0") {
        return tlResp(
          [
            snapshotItem({
              item_id: "s1.seg.0.0",
              kind: "inbound_compact_summary",
              payload: "previous summary",
            }),
            snapshotItem({ item_id: "s1.seg.4.0", kind: "agent_chat", payload: "previous session" }),
          ],
          true,
        );
      }
      if (opts?.before === "s1.seg.0.0") {
        return tlResp(
          [snapshotItem({ item_id: "s2.seg.4.0", kind: "agent_chat", payload: "older session" })],
          false,
        );
      }
      if (opts?.before !== undefined) return tlResp([], false);
      return tlResp(postWindow(), true);
    };
    vi.mocked(api.getTimeline).mockImplementation((_id: number, opts?: { before?: string; limit?: number }) =>
      Promise.resolve(pageFor(opts)),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(3));
    expect(result.current.hasMoreOlder).toBe(true);
    await waitFor(() => expect(queryClient.getQueryData(SETTINGS_QUERY_KEY)).toBeTruthy());

    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 3,
      items: postWindow(),
    });

    await waitFor(() => {
      expect(api.getTimeline).toHaveBeenCalledWith(42, { before: "s1.seg.0.0", limit: 100, signal: expect.any(AbortSignal) as AbortSignal });
    });
    await waitFor(() => {
      expect(result.current.items.map((i) => i.item_id)).toEqual([
        "s2.seg.4.0",
        "s1.seg.0.0",
        "s1.seg.4.0",
        "0.0",
        "1.0",
        "2.0",
      ]);
    });
    // Newest-first walk: the just-compacted session, then the one before it.
    expect(fetches.filter((b) => b !== undefined)).toEqual(["2.0", "s1.seg.0.0"]);
    expect(showError).not.toHaveBeenCalled();
  });

  it("All walks through a long segment and more than three previous pages, then stops at has_more=false", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ settings: [
      { key: "display.compact_history_sessions", value: -1, updated_at: "2026-09-24T00:00:00Z" },
    ] });
    const pages: Record<string, TimelineResponse> = {
      "2.0": tlResp([snapshotItem({ item_id: "1.5", payload: "same long segment" })], true),
      "1.5": tlResp([snapshotItem({ item_id: "s1.a.0.0", kind: "inbound_compact_summary", payload: "first" })], true),
      "s1.a.0.0": tlResp([snapshotItem({ item_id: "s2.b.0.0", kind: "inbound_compact_summary", payload: "second" })], true),
      "s2.b.0.0": tlResp([snapshotItem({ item_id: "s3.c.0.0", kind: "inbound_compact_summary", payload: "third" })], true),
      "s3.c.0.0": tlResp([snapshotItem({ item_id: "s4.d.0.0", kind: "inbound_compact_summary", payload: "fourth" })], false),
    };
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => Promise.resolve(opts?.before ? pages[opts.before] ?? tlResp([], false) : tlResp(postWindow(), true)));
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(3));
    await waitFor(() => expect(queryClient.getQueryData(SETTINGS_QUERY_KEY)).toBeTruthy());

    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 3, items: postWindow() });
    await waitFor(() => expect(result.current.items.some((item) => item.item_id === "s4.d.0.0")).toBe(true));
    const beforeCalls = () => vi.mocked(api.getTimeline).mock.calls.map(([, opts]) => opts?.before).filter((before) => before !== undefined);
    expect(beforeCalls()).toEqual(["2.0", "1.5", "s1.a.0.0", "s2.b.0.0", "s3.c.0.0"]);
    act(() => useTimelineStore.setState((state) => ({ items: [...state.items, snapshotItem({ item_id: "3.0" })] })));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(beforeCalls()).toHaveLength(5);
  });

  it("All waits for the post-compact tail read before deciding whether older history exists", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ settings: [
      { key: "display.compact_history_sessions", value: -1, updated_at: "2026-09-24T00:00:00Z" },
    ] });
    let postTailResolve: ((value: TimelineResponse) => void) | null = null;
    let postCompact = false;
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => {
      if (opts?.before) return Promise.resolve(tlResp([
        snapshotItem({ item_id: "s1.a.1.0", payload: "retained" }),
      ], false));
      if (!postCompact) return Promise.resolve(tlResp([
        snapshotItem({ item_id: "90.0", payload: "before compact" }),
      ], false));
      return new Promise<TimelineResponse>((resolve) => { postTailResolve = resolve; });
    });
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("before compact"));
    await waitFor(() => expect(queryClient.getQueryData(SETTINGS_QUERY_KEY)).toBeTruthy());
    postCompact = true;
    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 3, items: postWindow() });
    await waitFor(() => expect(postTailResolve).not.toBeNull());
    expect(vi.mocked(api.getTimeline).mock.calls.filter(([, opts]) => opts?.before)).toHaveLength(0);
    act(() => { postTailResolve!(tlResp(postWindow(), true)); });
    await waitFor(() => expect(result.current.items.some((item) => item.payload === "retained")).toBe(true));
    expect(vi.mocked(api.getTimeline).mock.calls.filter(([, opts]) => opts?.before)).toHaveLength(1);
  });

  it("All retries a failed history page and stops after the successful terminal page", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ settings: [
      { key: "display.compact_history_sessions", value: -1, updated_at: "2026-09-24T00:00:00Z" },
    ] });
    let olderAttempts = 0;
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => {
      if (!opts?.before) return Promise.resolve(tlResp(postWindow(), true));
      olderAttempts += 1;
      if (olderAttempts === 1) return Promise.reject(new Error("transient"));
      return Promise.resolve(tlResp([snapshotItem({ item_id: "s1.a.1.0", payload: "recovered" })], false));
    });
    const showError = vi.fn();
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(3));
    await waitFor(() => expect(queryClient.getQueryData(SETTINGS_QUERY_KEY)).toBeTruthy());
    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 3, items: postWindow() });
    await waitFor(() => expect(olderAttempts).toBe(1));
    act(() => useTimelineStore.setState((state) => ({ items: [...state.items, snapshotItem({ item_id: "3.0" })] })));
    await waitFor(() => expect(result.current.items.some((item) => item.payload === "recovered")).toBe(true));
    expect(olderAttempts).toBe(2);
    expect(showError).toHaveBeenCalledWith("Failed to load older messages: transient");
  });

  it("display.compact_history_sessions = 0 keeps the legacy clear-on-compact behavior (no retention fetch)", async () => {
    const showError = vi.fn();
    vi.mocked(api.getSettings).mockResolvedValue({
      settings: [
        { key: "display.compact_history_sessions", value: 0, updated_at: "2026-09-17T00:00:00Z" },
      ],
    });
    const postItems = [
      snapshotItem({ item_id: "0.0", kind: "system_prompt", payload: "PROMPT" }),
      snapshotItem({ item_id: "1.0", kind: "agent_chat", payload: "post-compact" }),
    ];
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp(postItems, true));
    vi.mocked(api.getTimeline).mockResolvedValueOnce(
      tlResp([snapshotItem({ item_id: "90.0", kind: "inbound_chat", payload: "pre-compact" })], true),
    );

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    // The settings query must land before the compact so the knob reads 0.
    await waitFor(() => expect(queryClient.getQueryData(SETTINGS_QUERY_KEY)).toBeTruthy());

    pushEvent({ role: "compact_done", agent_id: 42 });
    pushEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 2, items: postItems });
    await new Promise((r) => setTimeout(r, 20));

    const beforeCalls = vi
      .mocked(api.getTimeline)
      .mock.calls.filter((call) => call[1]?.before !== undefined);
    expect(beforeCalls).toHaveLength(0);
    expect(result.current.items.map((i) => i.item_id)).toEqual(["0.0", "1.0"]);
  });

  it("the post-compact snapshot also invalidates the agent's inspect / pending / token-usage caches (task #1959)", async () => {
    // Only the selected context owns detail reads.
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledTimes(1));
    invalidateSpy.mockClear();

    // The selected compact waits for its nonempty replacement snapshot.
    pushEvent({ role: "compact_done", agent_id: 42 });
    await new Promise((r) => setTimeout(r, 0));
    expect(invalidateSpy).not.toHaveBeenCalled();

    // The first non-empty post-compact snapshot fires the whole family set.
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 2,
      items: [snapshotItem({ item_id: "1.0", payload: "[summary]" })],
    });
    await waitFor(() => expect(invalidateSpy).toHaveBeenCalled());
    const keys = invalidateSpy.mock.calls.map((c) => c[0]?.queryKey);
    expect(keys).toEqual(
      expect.arrayContaining([
        ["timeline", 42],
        ["agent-inspect-live", 42],
        ["agent-inspect", 42],
        ["pending", 42],
        ["token-usage", 42],
      ]),
    );
  });
});

describe("useTimeline SSE inbound flow", () => {
  it("notes every event role that starts the timeline turn state", async () => {
    const showError = vi.fn();
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    const starts: SystemEvent[] = [
      { role: "inbound_arrived", agent_id: 42, inbound_id: 1, kind: "chat", source: "user", content: "hi" },
      { role: "chat_start", agent_id: 42, item_id: "1.0" },
      { role: "code_start", agent_id: 42, item_id: "1.1" },
      { role: "reasoning_start", agent_id: 42, item_id: "1.2" },
      { role: "exec_start", agent_id: 42, item_id: "1.3" },
    ];
    for (const event of starts) pushEvent(event);

    expect(noteTurnStart).toHaveBeenCalledTimes(starts.length);
    expect(vi.mocked(noteTurnStart).mock.calls).toEqual(starts.map(() => [42]));
  });

  it("inbound_arrived no longer produces an optimistic placeholder — turnActive flips true immediately; items unchanged", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp([
      snapshotItem({ kind: "code_output", payload: "previous" }),
    ]));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    pushEvent({
      role: "inbound_arrived",
      agent_id: 42,
      inbound_id: 100,
      kind: "chat",
      source: "user",
      content: "hello idle agent",
    });

    // Optimistic update removed — inbound_arrived inserts no placeholder; items still 1
    expect(result.current.items).toHaveLength(1);
    // turnActive still flips true immediately (managed by store.processSseEvent)
    expect(result.current.turnActive).toBe(true);
    // A paused impersonation has no native commit event; re-read server history.
    expect(api.getTimeline).toHaveBeenCalledTimes(2);
  });

  it("timeline_snapshot push delivers envelope-wrap inbound_chat into the timeline", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([
      snapshotItem({ item_id: "1.0", kind: "code_output", payload: "previous" }),
    ]));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    pushEvent({
      role: "inbound_arrived",
      agent_id: 42,
      inbound_id: 100,
      kind: "chat",
      source: "user",
      content: "msg",
    });

    // After receiving AgentStateChanged, the gateway pushes timeline_snapshot containing envelope-wrap inbound_chat
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ item_id: "1.0", kind: "code_output", payload: "previous" }),
        snapshotItem({
          item_id: "2.0",
          kind: "inbound_chat",
          payload: "User:\nmsg",
          inbound_id: 100,
        }),
      ],
    });

    await waitFor(() => {
      const inbound = result.current.items.find((i) => i.inbound_id === 100);
      expect(inbound?.payload).toContain("User:");
    });
    expect(result.current.items).toHaveLength(2);
  });
});

describe("useTimeline SSE LLM streaming flow", () => {
  it("partial chat → timeline_snapshot replaces the partial", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([
      snapshotItem({ item_id: "1.0", kind: "inbound_chat", payload: "user msg", inbound_id: 1 }),
    ]));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    pushEvent({
      role: "chat_delta",
      item_id: "2.0",
      agent_id: 42,
      content: "...continued reply",
    });
    expect(result.current.items[1]).toMatchObject({
      kind: "agent_chat",
      payload: "...continued reply",
      partial: true,
    });

    // Gateway pushes timeline_snapshot to replace the partial
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ item_id: "1.0", kind: "inbound_chat", payload: "user msg", inbound_id: 1 }),
        snapshotItem({ item_id: "2.0", kind: "agent_chat", payload: "Here's my full reply ...continued reply" }),
      ],
    });

    await waitFor(() => {
      expect(result.current.items[1].partial).toBeUndefined();
      expect(result.current.items[1].payload).toContain("Here's my full reply");
    });
  });

  it("streaming reasoning → timeline_snapshot replaces", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([]));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(0));

    pushEvent({ role: "reasoning_start", item_id: "2.0", agent_id: 42 });
    pushEvent({ role: "reasoning_delta", item_id: "2.0", agent_id: 42, content: "thinking..." });
    expect(result.current.items[0]).toMatchObject({
      kind: "agent_reasoning",
      payload: "thinking...",
    });

    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ item_id: "2.0", kind: "agent_reasoning", payload: "thinking... done" }),
      ],
    });

    await waitFor(() => {
      expect(result.current.items[0].payload).toBe("thinking... done");
    });
  });

  it("user sends a message mid-stream → timeline_snapshot fixes up", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([
      snapshotItem({ kind: "code_output", payload: "old" }),
    ]));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    pushEvent({ role: "reasoning_start", item_id: "test", agent_id: 42 });
    pushEvent({ role: "reasoning_delta", item_id: "test", agent_id: 42, content: "Let me think " });
    pushEvent({ role: "reasoning_delta", item_id: "test", agent_id: 42, content: "about it..." });
    expect(result.current.items[1]).toMatchObject({
      kind: "agent_reasoning",
      payload: "Let me think about it...",
    });

    pushEvent({
      role: "inbound_arrived",
      agent_id: 42,
      inbound_id: 200,
      kind: "chat",
      source: "user",
      content: "wait, also...",
    });

    // timeline_snapshot contains the full commit version
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ kind: "code_output", payload: "old" }),
        snapshotItem({ kind: "agent_reasoning", payload: "Let me think about it... done" }),
        snapshotItem({ kind: "agent_chat", payload: "OK", inbound_id: null }),
      ],
    });

    await waitFor(() => {
      const reasoning = result.current.items.find((i) => i.kind === "agent_reasoning");
      expect(reasoning?.payload).toBe("Let me think about it... done");
      expect(reasoning?.partial).toBeUndefined();
    });
  });
});

describe("useTimeline error & cancellation paths", () => {
  it("getTimeline initial load failure clears partials", async () => {
    const showError = vi.fn();
    // First load fails
    vi.mocked(api.getTimeline).mockRejectedValueOnce(new Error("network down"));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });

    // partial chat (user joined mid-stream and missed start)
    pushEvent({ role: "chat_delta", item_id: "test", agent_id: 42, content: "in progress" });
    expect(result.current.items[0]).toMatchObject({ partial: true });

    // query error triggers clearPartialFlags
    await waitFor(() => expect(showError).toHaveBeenCalled());
    await waitFor(() => {
      expect(result.current.items[0]?.partial).toBeFalsy();
    });
    expect(result.current.items[0]?.payload).toBe("in progress");
  });

  it("cancelled flag race: after agent switch, old fetch resolve does not contaminate new agent items", async () => {
    const showError = vi.fn();
    // First fetch intentionally hangs; deferred resolver fires only after the agent switch
    let resolveStaleA: (v: TimelineResponse) => void = () => { /* noop */ };
    vi.mocked(api.getTimeline).mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolveStaleA = r;
        }),
    );
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([
      snapshotItem({ payload: "thread B fresh" }),
    ]));

    const { result, rerender, unmount } = renderHook(
      ({ tid }: { tid: number | null }) => useTimeline(tid, showError),
      { initialProps: { tid: 1 }, wrapper },
    );

    // Switch agents (A's fetch still pending)
    rerender({ tid: 2 });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("thread B fresh"));

    // Now resolve old thread A's stale snapshot — cancelled flag should make it a no-op
    resolveStaleA(tlResp([snapshotItem({ payload: "STALE THREAD A" })]));
    await new Promise((r) => setTimeout(r, 10));

    // thread A's stale snapshot does not contaminate thread B's items
    expect(result.current.items.find((i) => i.payload === "STALE THREAD A")).toBeUndefined();
    expect(result.current.items[0].payload).toBe("thread B fresh");
    unmount();
  });
});

describe("useTimeline connectionState", () => {
  it("default connectionState='open'", async () => {
    const showError = vi.fn();
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());
    expect(result.current.connectionState).toBe("open");
  });

  it("reconnecting raises no toast; restores after open", async () => {
    const showError = vi.fn();
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    pushConnectionEvent({ type: "reconnecting" });
    expect(result.current.connectionState).toBe("reconnecting");
    expect(showError).not.toHaveBeenCalled();

    pushConnectionEvent({ type: "open" });
    expect(result.current.connectionState).toBe("open");
  });

  it("closed only changes state, no toast (single-channel banner, avoids double-noise)", async () => {
    const showError = vi.fn();
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    pushConnectionEvent({ type: "closed" });
    expect(result.current.connectionState).toBe("closed");
    expect(showError).not.toHaveBeenCalled();
  });

  it("on closed, partial flags cleared + streamingCode=false (SSE dead, stream won't resume)", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([]));
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toEqual([]));

    // User joins mid-stream (code and chat have independent item_ids; avoid collision)
    pushEvent({ role: "code_start", item_id: "5.1", agent_id: 42 });
    pushEvent({ role: "code_delta", item_id: "5.1", agent_id: 42, content: "x = 1" });
    expect(result.current.streamingCode).toBe(true);

    // Use a partial item to simulate a chat_delta that missed start (different id, creates a partial)
    act(() => {
      currentEventHandler!({
        role: "chat_delta",
        item_id: "5.0",
        agent_id: 42,
        content: "...",
      });
    });
    expect(result.current.items.find((i) => i.partial)).toBeDefined();

    pushConnectionEvent({ type: "closed" });
    // partial flag preserved (showing content as "..." is reasonable),
    // but add the interrupted flag so the timeline renders a
    // "streaming interrupted" hint that distinguishes "the message
    // ends here" from "the connection died". streamingCode still has
    // to flip back (SSE is dead; stream won't resume).
    expect(result.current.items.find((i) => i.partial)).toBeDefined();
    expect(result.current.items.find((i) => i.interrupted)).toBeDefined();
    expect(result.current.streamingCode).toBe(false);

    // After reconnect (open event), interrupted flag is cleared —
    // SSE will keep pushing deltas appended to this partial item;
    // the "interrupted" hint no longer applies.
    pushConnectionEvent({ type: "open" });
    expect(result.current.items.find((i) => i.interrupted)).toBeUndefined();
    // partial still preserved — only chat_done / code_done truly clears it
    expect(result.current.items.find((i) => i.partial)).toBeDefined();
  });

  it("on agentId switch, connectionState is PRESERVED (Task #1051: the all-events stream is one shared connection — switchThread no longer stamps 'open', which would clear the disconnect banner mid-reconnect)", async () => {
    const showError = vi.fn();
    const { result, rerender, unmount } = renderHook(
      ({ tid }: { tid: number | null }) => useTimeline(tid, showError),
      { initialProps: { tid: 1 }, wrapper },
    );
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    pushConnectionEvent({ type: "closed" });
    expect(result.current.connectionState).toBe("closed");

    rerender({ tid: 2 });
    // The shared all-events connection is still closed — switching agents
    // must not pretend the stream came back.
    expect(result.current.connectionState).toBe("closed");

    // A healthy switch keeps "open" too.
    pushConnectionEvent({ type: "open" });
    rerender({ tid: 3 });
    expect(result.current.connectionState).toBe("open");
    unmount();
  });

  it("opening the selected stream reconciles through one composed snapshot read", async () => {
    vi.mocked(api.getTimeline).mockResolvedValue(
      tlResp([snapshotItem({ item_id: "1.0", payload: "initial read" })]),
    );
    vi.mocked(api.getConversationSnapshot).mockReset().mockResolvedValue(
      composedSnapshot(tlResp([
        snapshotItem({ item_id: "1.0", payload: "reconciled after gap" }),
        snapshotItem({ item_id: "2.0", payload: "committed while away" }),
      ])),
    );
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("initial read"));

    pushConnectionEvent({ type: "open" });

    await waitFor(() =>
      expect(result.current.items.map((item) => item.payload)).toEqual(
        ["reconciled after gap", "committed while away"],
      ),
    );
    expect(api.getConversationSnapshot).toHaveBeenCalledTimes(1);
    // The per-domain trailing repair is replaced, not stacked: the composed
    // read is the only read the re-attach cost.
    expect(api.getTimeline).toHaveBeenCalledTimes(1);
  });

  it("open with no active agent does not reconcile a snapshot", async () => {
    renderHook(() => useTimeline(null, vi.fn()), { wrapper });

    pushConnectionEvent({ type: "open" });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });

    expect(api.getConversationSnapshot).not.toHaveBeenCalled();
  });

  it("parse-failed: first toast fires; subsequent same-error toasts deduped (no flood)", async () => {
    const showError = vi.fn();
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    const sameError = new SyntaxError("Unexpected token");
    pushConnectionEvent({ type: "parse-failed", raw: "x", error: sameError });
    pushConnectionEvent({ type: "parse-failed", raw: "y", error: sameError });
    pushConnectionEvent({ type: "parse-failed", raw: "z", error: sameError });

    // dedupe by error.toString — floods do not crowd out other toasts
    const parseFails = showError.mock.calls.filter((c) =>
      String(c[0]).includes("SSE event parse failed"),
    );
    expect(parseFails).toHaveLength(1);
  });

  it("parse-failed different errors → each reported once", async () => {
    const showError = vi.fn();
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    pushConnectionEvent({
      type: "parse-failed",
      raw: "x",
      error: new SyntaxError("err A"),
    });
    pushConnectionEvent({
      type: "parse-failed",
      raw: "y",
      error: new SyntaxError("err B"),
    });

    const parseFails = showError.mock.calls.filter((c) =>
      String(c[0]).includes("SSE event parse failed"),
    );
    expect(parseFails).toHaveLength(2);
  });

  it("open event resets the parse-failed dedupe set (new connection re-enables toast)", async () => {
    const showError = vi.fn();
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    const err = new SyntaxError("dup");
    pushConnectionEvent({ type: "parse-failed", raw: "x", error: err });
    pushConnectionEvent({ type: "open" });
    pushConnectionEvent({ type: "parse-failed", raw: "y", error: err });

    const parseFails = showError.mock.calls.filter((c) =>
      String(c[0]).includes("SSE event parse failed"),
    );
    expect(parseFails).toHaveLength(2); // open re-allows reporting
  });

  it("showError prop switch → new showError receives parse-failed toast (kills L209 dep [])", async () => {
    // cover use-timeline.ts L209 `[showError, processConnectionEvent]` dep array.
    // If the dep is changed to `[]`, onConnectionEvent forever
    // closes over the first showError; even when the prop changes,
    // the closure does not refresh — the new showError receives
    // nothing while the old one keeps firing. This test pins that
    // the dep array must track showError.
    const firstShowError = vi.fn();
    const secondShowError = vi.fn();
    const { rerender } = renderHook(
      ({ se }: { se: (msg: string) => void }) => useTimeline(42, se),
      { initialProps: { se: firstShowError }, wrapper },
    );
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    rerender({ se: secondShowError });

    pushConnectionEvent({
      type: "parse-failed",
      raw: "x",
      error: new SyntaxError("after rebind"),
    });

    // New showError should fire; old showError no longer receives parse-failed
    const newCalls = secondShowError.mock.calls.filter((c) =>
      String(c[0]).includes("SSE event parse failed"),
    );
    const oldCalls = firstShowError.mock.calls.filter((c) =>
      String(c[0]).includes("SSE event parse failed"),
    );
    expect(newCalls).toHaveLength(1);
    expect(oldCalls).toHaveLength(0);
  });
});

describe("useTimeline streamingCode flag", () => {
  it("code_start sets streamingCode=true; exec_start clears it back to false", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    expect(result.current.streamingCode).toBe(false);
    pushEvent({ role: "code_start", item_id: "test", agent_id: 42 });
    expect(result.current.streamingCode).toBe(true);
    pushEvent({ role: "exec_start", item_id: "5.0", agent_id: 42 });
    expect(result.current.streamingCode).toBe(false);
  });
});

describe("useTimeline token_usage (single subscription — R10)", () => {
  it("token_usage is NOT folded by useTimeline — the token state is owned solely by useTokenUsage", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));

    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());
    // The switch effect made 42 the active thread, so a token_usage for 42
    // would pass isEventForThread — the only reason it stays untouched is the
    // early return in onSystemEvent (before the fix, useTimeline ALSO ran
    // processSseEvent here, double-writing the token state alongside
    // useTokenUsage).
    expect(useTimelineStore.getState().activeThreadId).toBe(42);
    useTimelineStore.setState({ tokenUsage: 0, reasoningTokens: 0 });

    pushEvent({
      role: "token_usage",
      agent_id: 42,
      input_tokens: 1234,
      output_tokens: 0,
      reasoning_tokens: 56,
    });

    // useTimeline ignored it → store token fields still at the baseline.
    expect(useTimelineStore.getState().tokenUsage).toBe(0);
    expect(useTimelineStore.getState().reasoningTokens).toBe(0);
  });
});

describe("useTimeline full two-turn conversation overall order (e2e)", () => {
  it("both turns pushed via timeline_snapshot; order strictly aligned by item_id", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([]));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(0));

    // Turn 1: user sends a message
    pushEvent({ role: "inbound_arrived", agent_id: 42, inbound_id: 100, kind: "chat", source: "user", content: "turn 1" });

    // Turn 1 agent streaming
    pushEvent({ role: "reasoning_start", item_id: "3.0", agent_id: 42 });
    pushEvent({ role: "reasoning_delta", item_id: "3.0", agent_id: 42, content: "think1" });
    pushEvent({ role: "chat_start", item_id: "3.1", agent_id: 42 });
    pushEvent({ role: "chat_delta", item_id: "3.1", agent_id: 42, content: "reply1" });
    pushEvent({ role: "code_start", item_id: "3.2", agent_id: 42 });
    pushEvent({ role: "code_delta", item_id: "3.2", agent_id: 42, content: "code1" });
    pushEvent({ role: "exec_start", item_id: "4.0", agent_id: 42 });
    pushEvent({ role: "exec_output", item_id: "4.0", agent_id: 42, content: "output1" });

    // Gateway pushes timeline_snapshot
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ item_id: "2.0", kind: "inbound_chat", payload: "turn 1 env", inbound_id: 100 }),
        snapshotItem({ item_id: "3.0", kind: "agent_reasoning", payload: "think1" }),
        snapshotItem({ item_id: "3.1", kind: "agent_chat", payload: "reply1" }),
        snapshotItem({ item_id: "3.2", kind: "agent_code", payload: "code1" }),
        snapshotItem({ item_id: "4.0", kind: "code_output", payload: "output1" }),
      ],
    });

    await waitFor(() => {
      expect(result.current.items).toHaveLength(5);
      const kinds = result.current.items.map((i) => i.kind);
      expect(kinds).toEqual(["inbound_chat", "agent_reasoning", "agent_chat", "agent_code", "code_output"]);
      const ids = result.current.items.map((i) => i.item_id);
      expect(ids).toEqual(["2.0", "3.0", "3.1", "3.2", "4.0"]);
    });

    // Turn 2
    pushEvent({ role: "inbound_arrived", agent_id: 42, inbound_id: 101, kind: "chat", source: "user", content: "turn 2" });
    pushEvent({ role: "reasoning_start", item_id: "6.0", agent_id: 42 });
    pushEvent({ role: "reasoning_delta", item_id: "6.0", agent_id: 42, content: "think2" });
    pushEvent({ role: "chat_start", item_id: "6.1", agent_id: 42 });
    pushEvent({ role: "chat_delta", item_id: "6.1", agent_id: 42, content: "reply2" });

    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ item_id: "2.0", kind: "inbound_chat", payload: "turn 1 env", inbound_id: 100 }),
        snapshotItem({ item_id: "3.0", kind: "agent_reasoning", payload: "think1" }),
        snapshotItem({ item_id: "3.1", kind: "agent_chat", payload: "reply1" }),
        snapshotItem({ item_id: "3.2", kind: "agent_code", payload: "code1" }),
        snapshotItem({ item_id: "4.0", kind: "code_output", payload: "output1" }),
        snapshotItem({ item_id: "5.0", kind: "inbound_chat", payload: "turn 2 env", inbound_id: 101 }),
        snapshotItem({ item_id: "6.0", kind: "agent_reasoning", payload: "think2" }),
        snapshotItem({ item_id: "6.1", kind: "agent_chat", payload: "reply2" }),
      ],
    });

    await waitFor(() => {
      expect(result.current.items).toHaveLength(8);
      const kinds = result.current.items.map((i) => i.kind);
      expect(kinds).toEqual([
        "inbound_chat", "agent_reasoning", "agent_chat", "agent_code", "code_output",
        "inbound_chat", "agent_reasoning", "agent_chat",
      ]);
    });
  });

  it("when snapshot contains the commit version, replaces partial (item_id matches)", async () => {
    // New semantics (hard reset): snapshot is the only truth. The
    // old "partial preserved" test no longer applies — hard reset
    // does not keep stable-id partials. This case only verifies
    // "partial with the same item_id is replaced by the snapshot
    // commit version", a behavior consistent with both versions.
    const showError = vi.fn();
    // Initial mock returns identically to the final snapshot to avoid the hook data-effect race
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([
      snapshotItem({ item_id: "2.0", kind: "inbound_chat", payload: "turn 1 env", inbound_id: 100 }),
    ]));

    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    // SSE push of the commit version with the same item_id → snapshot replaces it
    pushEvent({
      role: "timeline_snapshot",
      agent_id: 42,
      msg_count: 0,
      items: [
        snapshotItem({ item_id: "2.0", kind: "inbound_chat", payload: "turn 1 env", inbound_id: 100 }),
        snapshotItem({ item_id: "3.0", kind: "agent_chat", payload: "reply commit" }),
      ],
    });

    await waitFor(() => {
      expect(result.current.items).toHaveLength(2);
      expect(result.current.items[1].item_id).toBe("3.0");
      expect(result.current.items[1].partial).toBeUndefined();
    });
  });
});

describe("useTimeline startTransition split (isStreamingDelta cluster)", () => {
  // Pins use-timeline.ts L166-178 isStreamingDelta check +
  // startTransition split. Both if-branches call processSseEvent(ev),
  // so store terminal state is identical → asserting items state
  // alone cannot kill the mutation. The only observable difference
  // is startTransition call count:
  //   - streaming delta (code/chat/reasoning/exec_output_chunk) → must wrap in startTransition
  //   - non-streaming (lifecycle / snapshot / start etc.) → dispatch directly, no wrap
  // See startTransitionSpy at the top of the file.

  beforeEach(() => {
    startTransitionSpy.mockClear();
  });

  it.each([
    ["code_delta", { role: "code_delta", item_id: "1", agent_id: 42, content: "x" }],
    ["chat_delta", { role: "chat_delta", item_id: "1", agent_id: 42, content: "y" }],
    ["reasoning_delta", { role: "reasoning_delta", item_id: "1", agent_id: 42, content: "z" }],
    ["exec_output_chunk", { role: "exec_output_chunk", item_id: "1", agent_id: 42, content: "w" }],
  ] as const)(
    "%s wraps in startTransition (kills L167-172 streaming role / equality / if-branch mutations)",
    async (_label, ev) => {
      const showError = vi.fn();
      vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));
      renderHook(() => useTimeline(42, showError), { wrapper });
      await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

      startTransitionSpy.mockClear();
      pushEvent(ev);

      // Wrap startTransition only for streaming-delta events —
      // precisely pins the if-branch true path. Changing any of the
      // four role strings ("" / other literal) makes isStreamingDelta
      // false → spy call count 0 → test fails.
      expect(startTransitionSpy).toHaveBeenCalledTimes(1);
    },
  );

  it.each([
    ["reasoning_start", { role: "reasoning_start", item_id: "1", agent_id: 42 }],
    ["chat_start", { role: "chat_start", item_id: "1", agent_id: 42 }],
    ["code_start", { role: "code_start", item_id: "1", agent_id: 42 }],
    ["exec_start", { role: "exec_start", item_id: "5.0", agent_id: 42 }],
    ["llm_done", { role: "llm_done", agent_id: 42 }],
    [
      "inbound_arrived",
      {
        role: "inbound_arrived",
        agent_id: 42,
        inbound_id: 1,
        kind: "chat",
        source: "user",
        content: "hi",
      },
    ],
  ] as const)(
    "%s does not go through startTransition (kills L167-172 if-branch false path + role equality reverse mutation)",
    async (_label, ev) => {
      const showError = vi.fn();
      vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));
      renderHook(() => useTimeline(42, showError), { wrapper });
      await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

      startTransitionSpy.mockClear();
      pushEvent(ev);

      // Non-streaming events must dispatch directly — any role
      // equality flip (`===` → `!==`) would make some non-streaming
      // role wrongly match isStreamingDelta=true → spy gets called →
      // test fail.
      expect(startTransitionSpy).toHaveBeenCalledTimes(0);
    },
  );

  it("if(isStreamingDelta) → true mutation: non-streaming events also wrongly wrapped (kills L172 true)", async () => {
    // Explicitly pin L172 → true mutation again — `if (true)` makes
    // every event (including lifecycle) take the startTransition path.
    // The it.each above already covers this, but add an explicit
    // assertion as a safety net.
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    startTransitionSpy.mockClear();
    pushEvent({ role: "exec_start", item_id: "5.0", agent_id: 42 });

    expect(startTransitionSpy).not.toHaveBeenCalled();
  });

  it("if(isStreamingDelta) → false mutation: streaming events miss transition (kills L172 false)", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    startTransitionSpy.mockClear();
    pushEvent({ role: "code_delta", item_id: "1", agent_id: 42, content: "x" });

    expect(startTransitionSpy).toHaveBeenCalledTimes(1);
  });
});

describe("useTimeline data effect agentId==null guard (L135)", () => {
  it("agentId=null with pre-filled query cache: data effect does not call reloadSnapshot (kills L135 agentId==null → false)", async () => {
    // cover use-timeline.ts L135 `if (!timelineQuery.data || agentId == null) return;`
    // Mutation: `agentId == null` → `false` defeats the short-circuit.
    // When agentId=null, useQuery `enabled: false` normally does not
    // enter the data path. But pre-filling ["timeline", null] via
    // queryClient.setQueryData makes useQuery return that cached
    // data even with enabled=false — at that point
    // `!timelineQuery.data` is false, and only `agentId == null`
    // forces the short-circuit.
    // Original behavior: agentId==null → short-circuits → reloadSnapshot not called.
    // Mutation behavior: agentId==null → false → no return →
    //   reloadSnapshot(data) gets called — that's the observable diff.
    const showError = vi.fn();
    const reloadSpy = vi.spyOn(useTimelineStore.getState(), "reloadSnapshot");
    useTimelineStore.setState({
      reloadSnapshot: reloadSpy,
    });

    // Pre-fill the null queryKey cache
    queryClient.setQueryData<TimelineResponse>(
      ["timeline", null],
      tlResp([snapshotItem({ payload: "prefilled null cache" })]),
    );

    renderHook(() => useTimeline(null, showError), { wrapper });

    // Wait for React to run all effects
    await new Promise((r) => setTimeout(r, 0));

    // agentId==null should short-circuit → reloadSnapshot not called.
    // After mutation: short-circuit defeated → reloadSnapshot called.
    expect(reloadSpy).not.toHaveBeenCalled();
  });
});


describe("useTimeline SSE batch path", () => {
  it("a frame batch folds into the expected final timeline state", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockReset().mockResolvedValue(tlResp([]));
    const { result } = renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());
    // Settle the initial snapshot fold before exercising the frame batch.
    await act(async () => {
      await Promise.resolve();
    });

    pushBatch([
      { role: "code_start", agent_id: 42, item_id: "7.0" },
      { role: "code_delta", agent_id: 42, item_id: "7.0", content: "pri" },
      { role: "code_delta", agent_id: 42, item_id: "7.0", content: "nt" },
      { role: "token_usage", agent_id: 42, input_tokens: 500, output_tokens: 10 },
    ]);
    // token_usage is owned by useTokenUsage and must not change this state.
    expect(result.current.items).toHaveLength(1);
    expect(result.current.items[0].item_id).toBe("7.0");
    expect(result.current.items[0].payload).toBe("print");
    expect(result.current.streamingCode).toBe(true);
    expect(noteTurnStart).toHaveBeenCalledWith(42);
  });
});

describe("impersonation timeline refresh", () => {
  it("loads committed external replies through the normal timeline query", async () => {
    const anchor = snapshotItem({ item_id: "4.0", kind: "inbound_chat", payload: "Session started" });
    const reply = snapshotItem({ item_id: "4.2", payload: "Fix verified", source: "agent:42",
      impersonation: { agent_id: 42, session_id: 0, name: "Fix login", executor_name: "Codex: helper",
        provider: "codex", process: {}, anchor_item_id: "4.0", seq: 1 } });
    vi.mocked(api.getTimeline).mockResolvedValueOnce(tlResp([anchor])).mockResolvedValue(tlResp([anchor, reply]));
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    pushEvent({ role: "impersonation_changed", agent_id: 42 });
    await waitFor(() => expect(result.current.items.map((entry) => entry.payload)).toContain("Fix verified"));
    pushEvent({ role: "impersonation_changed", agent_id: 42 });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledTimes(3));
    expect(result.current.items.filter((entry) => entry.item_id === "4.2")).toHaveLength(1);
    expect(result.current.items.find((entry) => entry.item_id === "4.2")?.impersonation?.executor_name).toBe("Codex: helper");
  });
});


describe("selected timeline ownership", () => {
  it("clearing selection releases displayed content and starts no request", async () => {
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp([snapshotItem({ payload: "selected" })]));
    const { result, rerender } = renderHook<ReturnType<typeof useTimeline>, { id: number | null }>(({ id }) => useTimeline(id, vi.fn()), {
      initialProps: { id: 1 }, wrapper,
    });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("selected"));
    const reads = vi.mocked(api.getTimeline).mock.calls.length;
    rerender({ id: null });
    expect(result.current.items).toEqual([]);
    expect(useTimelineStore.getState().activeThreadId).toBeNull();
    expect(api.getTimeline).toHaveBeenCalledTimes(reads);
  });

  it("rapid A to B to A aborts obsolete reads and cannot accept the first A response", async () => {
    const reads: { id: number; signal: AbortSignal; resolve: (data: TimelineResponse) => void }[] = [];
    vi.mocked(api.getTimeline).mockImplementation((id, opts) => new Promise((resolve) => {
      if (!opts?.signal) throw new Error("missing AbortSignal");
      reads.push({ id, signal: opts.signal, resolve });
    }));
    const { result, rerender } = renderHook(({ id }: { id: number }) => useTimeline(id, vi.fn()), {
      initialProps: { id: 1 }, wrapper,
    });
    await waitFor(() => expect(reads).toHaveLength(1));
    rerender({ id: 2 });
    await waitFor(() => expect(reads).toHaveLength(2));
    expect(reads[0].signal.aborted).toBe(true);
    rerender({ id: 1 });
    await waitFor(() => expect(reads).toHaveLength(3));
    expect(reads[1].signal.aborted).toBe(true);
    await act(async () => {
      reads[2].resolve(tlResp([snapshotItem({ item_id: "1.0", payload: "current A" })]));
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("current A"));
    await act(async () => {
      reads[0].resolve(tlResp([snapshotItem({ item_id: "1.0", payload: "obsolete A" })]));
      reads[1].resolve(tlResp([snapshotItem({ item_id: "1.0", payload: "obsolete B" })]));
      await Promise.resolve();
    });
    expect(result.current.items.map((item) => item.payload)).toEqual(["current A"]);
  });

  it("aborts old history pages even after switching back to the same agent", async () => {
    let finishPage!: (data: TimelineResponse) => void;
    let pageSignal: AbortSignal | undefined;
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => {
      if (opts?.before) {
        pageSignal = opts.signal;
        return new Promise((resolve) => { finishPage = resolve; });
      }
      return Promise.resolve(tlResp([snapshotItem({ item_id: "5.0", payload: "current" })], true));
    });
    const { result, rerender } = renderHook(({ id }: { id: number }) => useTimeline(id, showError), {
      initialProps: { id: 1 }, wrapper,
    });
    await waitFor(() => expect(result.current.hasMoreOlder).toBe(true));
    act(() => result.current.loadOlder());
    rerender({ id: 2 });
    expect(pageSignal?.aborted).toBe(true);
    rerender({ id: 1 });
    await act(async () => {
      finishPage(tlResp([snapshotItem({ item_id: "1.0", payload: "obsolete page" })]));
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.items.map((item) => item.payload)).toEqual(["current"]));
    expect(showError).not.toHaveBeenCalled();
  });

  it.each(["hidden", "compact"])("%s abandons a history request without accepting its late response", async (cause) => {
    let finishPage!: (data: TimelineResponse) => void;
    let pageSignal: AbortSignal | undefined;
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => {
      if (opts?.before) {
        pageSignal = opts.signal;
        return new Promise((resolve) => { finishPage = resolve; });
      }
      return Promise.resolve(tlResp([snapshotItem({ item_id: "5.0", payload: "current" })], true));
    });
    const { result } = renderHook(() => useTimeline(1, showError), { wrapper });
    await waitFor(() => expect(result.current.hasMoreOlder).toBe(true));
    act(() => result.current.loadOlder());
    expect(result.current.loadingOlder).toBe(true);
    const visibility = vi.spyOn(document, "visibilityState", "get");
    try {
      if (cause === "hidden") {
        act(() => {
          visibility.mockReturnValue("hidden");
          document.dispatchEvent(new Event("visibilitychange"));
        });
      } else {
        pushEvent({ role: "compact_done", agent_id: 1 });
      }
      expect(pageSignal?.aborted).toBe(true);
      expect(result.current.loadingOlder).toBe(false);
      await act(async () => {
        finishPage(tlResp([snapshotItem({ item_id: "1.0", payload: "obsolete page" })]));
        await Promise.resolve();
      });
      expect(result.current.items.map((item) => item.payload)).toEqual(["current"]);
      expect(showError).not.toHaveBeenCalled();
    } finally {
      visibility.mockRestore();
    }
  });

  it("returning after an inactive compact refreshes to the authoritative tail on reattach", async () => {
    let compacted = false;
    vi.mocked(api.getTimeline).mockImplementation((id) => Promise.resolve(
      tlResp([snapshotItem({ item_id: "1.0", payload: id === 1 && compacted ? "compacted A" : `agent ${id}` })]),
    ));
    vi.mocked(api.getConversationSnapshot).mockReset().mockImplementation(() =>
      Promise.resolve(composedSnapshot(tlResp([
        snapshotItem({ item_id: "1.0", payload: "compacted A" }),
      ]))));
    const { result, rerender } = renderHook(({ id }: { id: number }) => useTimeline(id, vi.fn()), {
      initialProps: { id: 1 }, wrapper,
    });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("agent 1"));
    rerender({ id: 2 });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("agent 2"));
    compacted = true;
    pushEvent({ role: "chat_delta", agent_id: 1, item_id: "8.0", content: "late old A" });
    rerender({ id: 1 });

    // The retained snapshot seeds the switch back; the per-domain read does
    // not re-run (task #3900 batch 2). A late delta for the thread was never
    // folded into it.
    await waitFor(() => expect(result.current.items.map((item) => item.payload)).toEqual(["agent 1"]));
    pushConnectionEvent({ type: "open" });
    await waitFor(() => expect(result.current.items.map((item) => item.payload)).toEqual(["compacted A"]));

    expect(api.getTimeline).toHaveBeenCalledTimes(2); // cold A + cold B only
    expect(api.getConversationSnapshot).toHaveBeenCalledTimes(1);
    // Both per-agent windows stay retained for a switch back.
    expect(queryClient.getQueryCache().findAll({ queryKey: ["timeline"] })).toHaveLength(2);
  });
});


describe("selected opening-gap reconcile", () => {
  it("reads again after a pre-open initial read settles", async () => {
    let finish!: (value: TimelineResponse) => void;
    vi.mocked(api.getTimeline)
      .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }))
      .mockResolvedValue(tlResp([snapshotItem({ item_id: "1.0", payload: "unexpected per-domain read" })]));
    vi.mocked(api.getConversationSnapshot).mockReset().mockResolvedValue(
      composedSnapshot(tlResp([snapshotItem({ item_id: "1.0", payload: "committed before stream opened" })])),
    );
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledTimes(1));
    pushConnectionEvent({ type: "open" });
    await act(async () => {
      finish(tlResp([snapshotItem({ item_id: "1.0", payload: "read before subscription" })]));
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("committed before stream opened"));
    // The trailing read joined the in-flight initial read (no overlap) and
    // came from the composed endpoint — the per-domain read never re-ran.
    expect(api.getConversationSnapshot).toHaveBeenCalledTimes(1);
    expect(api.getTimeline).toHaveBeenCalledTimes(1);
  });

  it("compact_done aborts an in-flight reconcile: the pre-compact snapshot never lands", async () => {
    vi.mocked(api.getTimeline).mockResolvedValue(
      tlResp([snapshotItem({ item_id: "1.0", payload: "stable" })]),
    );
    let finishSnapshot!: (value: ConversationSnapshotResponse) => void;
    vi.mocked(api.getConversationSnapshot).mockReset().mockImplementation(
      () => new Promise((resolve) => { finishSnapshot = resolve; }),
    );
    const { result } = renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("stable"));

    pushConnectionEvent({ type: "open" });
    await waitFor(() => expect(api.getConversationSnapshot).toHaveBeenCalledTimes(1));

    // A compact lands while the composed read is in flight. The read must be
    // aborted — a pre-compact snapshot must not overwrite the post-compact
    // window (the timeline query's own fetch is cancelled for the same reason).
    pushEvent({ role: "compact_done", agent_id: 42 });

    await act(async () => {
      finishSnapshot(composedSnapshot(
        tlResp([snapshotItem({ item_id: "1.0", payload: "pre-compact ghost" })]),
      ));
      await Promise.resolve();
    });
    expect(result.current.items.map((item) => item.payload)).toEqual(["stable"]);
    expect(queryClient.getQueryData<TimelineResponse>(["timeline", 42]))
      .toMatchObject({ items: [{ payload: "stable" }] });
  });
});


describe("compact retention request ownership", () => {
  it("a reconnect-only compact replacement reattaches the previous session once", async () => {
    let compacted = false;
    const tailWindow = () => tlResp(compacted ? postWindow() : [
      snapshotItem({ item_id: "90.0", payload: "before gap" }),
    ], true);
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => {
      if (opts?.before) return Promise.resolve(tlResp([
        snapshotItem({ item_id: "s1.gap.1.0", payload: "retained across reconnect" }),
      ], false));
      return Promise.resolve(tailWindow());
    });
    vi.mocked(api.getConversationSnapshot).mockReset().mockImplementation(() =>
      Promise.resolve(composedSnapshot(tailWindow())));
    const { result } = renderHook(() => useTimeline(1, vi.fn()), { wrapper });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("before gap"));
    compacted = true;
    pushConnectionEvent({ type: "open" });
    await waitFor(() => expect(result.current.items.map((item) => item.payload)).toContain("retained across reconnect"));
    expect(useTimelineStore.getState().compactReplaceSeq).toBe(1);
    pushConnectionEvent({ type: "open" });
    await waitFor(() => expect(api.getConversationSnapshot).toHaveBeenCalledTimes(2));
    expect(useTimelineStore.getState().compactReplaceSeq).toBe(1);
    // The re-attach refreshes only through the composed read; the single
    // `before` fetch is the retention's own cross-segment read.
    expect(api.getTimeline).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.getTimeline).mock.calls.filter((call) => call[1]?.before)).toHaveLength(1);
  });

  it.each(["switch", "hidden", "unmount", "clear"].flatMap((cause) => [2, -1].map((retention) => ({ cause, retention }))))("$cause cancels retention $retention and rejects its late completion", async ({ cause, retention }) => {
    const pages: { signal: AbortSignal; resolve: (page: TimelineResponse) => void }[] = [];
    const showError = vi.fn();
    vi.mocked(api.getSettings).mockResolvedValue({ settings: [
      { key: "display.compact_history_sessions", value: retention, updated_at: "2026-09-17T00:00:00Z" },
    ] });
    vi.mocked(api.getTimeline).mockImplementation((_id, opts) => {
      if (!opts?.before) return Promise.resolve(tlResp(postWindow(), true));
      if (!opts.signal) throw new Error("History requires cancellation");
      const signal = opts.signal;
      return new Promise((resolve) => { pages.push({ signal, resolve }); });
    });
    const view = renderHook<ReturnType<typeof useTimeline>, { id: number | null }>(
      ({ id }) => useTimeline(id, showError), { initialProps: { id: 1 }, wrapper },
    );
    await waitFor(() => expect(view.result.current.items).toHaveLength(3));
    await waitFor(() => expect(queryClient.getQueryData(SETTINGS_QUERY_KEY)).toBeTruthy());
    const compact = () => {
      pushEvent({ role: "compact_done", agent_id: 1 });
      pushEvent({ role: "timeline_snapshot", agent_id: 1, msg_count: 3, items: postWindow() });
    };
    compact();
    await waitFor(() => expect(pages).toHaveLength(1));
    const visibility = vi.spyOn(document, "visibilityState", "get");
    try {
      if (cause === "switch") {
        view.rerender({ id: 2 });
        view.rerender({ id: 1 });
        await waitFor(() => expect(view.result.current.items).toHaveLength(3));
        compact();
        // The new A owns a new loop even while the old A's server ignores abort.
        await waitFor(() => expect(pages).toHaveLength(2));
      } else if (cause === "hidden") {
        act(() => {
          visibility.mockReturnValue("hidden");
          document.dispatchEvent(new Event("visibilitychange"));
        });
      } else if (cause === "clear") {
        view.rerender({ id: null });
      } else {
        view.unmount();
      }
      expect(pages[0].signal.aborted).toBe(true);
      await act(async () => {
        pages[0].resolve(tlResp([snapshotItem({ item_id: "s1.old.1.0", payload: "obsolete retained page" })], true));
        await Promise.resolve();
      });
      expect(useTimelineStore.getState().items.some((item) => item.payload === "obsolete retained page")).toBe(false);
      expect(pages).toHaveLength(cause === "switch" ? 2 : 1);
      if (cause === "hidden") {
        act(() => {
          visibility.mockReturnValue("visible");
          document.dispatchEvent(new Event("visibilitychange"));
        });
        // Resume the same retention intent through a fresh abortable request.
        await waitFor(() => expect(pages).toHaveLength(2));
      }
      if (cause === "switch" || cause === "hidden") {
        expect(pages[1].signal.aborted).toBe(false);
        await act(async () => {
          pages[1].resolve(tlResp([snapshotItem({ item_id: "s1.new.1.0", payload: "current retained page" })], true));
          await Promise.resolve();
        });
        await waitFor(() => expect(pages).toHaveLength(3));
        await act(async () => {
          pages[2].resolve(tlResp([snapshotItem({ item_id: "s2.new.1.0", payload: "older retained page" })], false));
          await Promise.resolve();
        });
        expect(view.result.current.items.map((item) => item.payload)).toContain("older retained page");
      }
      expect(showError).not.toHaveBeenCalled();
    } finally {
      visibility.mockRestore();
    }
  });
});
