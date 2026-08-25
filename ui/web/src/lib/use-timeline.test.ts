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
import type { BackendTimelineItem, SystemEvent, TimelineResponse } from "./types";
import { parseItemId } from "./timeline";
import { useTimelineStore } from "./timeline-store";
import { useTimeline } from "./use-timeline";
import type { ConnectionEvent } from "./useEventStream";

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
function tlResp(items: BackendTimelineItem[] = [], has_more = false): TimelineResponse {
  let max = -1;
  for (const it of items) {
    const parsed = parseItemId(it.item_id);
    if (parsed && parsed[0] > max) max = parsed[0];
  }
  return { items, msg_count: max + 1, has_more };
}

beforeEach(() => {
  vi.clearAllMocks();
  currentEventHandler = null;
  currentConnectionHandler = null;
  currentBatchHandler = null;
  // Default getTimeline returns empty; each test resets as needed
  vi.mocked(api.getTimeline).mockResolvedValue(tlResp([]));
  // Restore any store actions a prior test swapped for spies, so switchThread /
  // reloadSnapshot are the real (stable) references again before this test runs.
  useTimelineStore.setState({ switchThread: REAL_SWITCH_THREAD, reloadSnapshot: REAL_RELOAD_SNAPSHOT });
  // Reset Zustand store to prevent cross-test contamination — the
  // store is a module-level singleton; previous tests' items /
  // connectionState leak into the next test. Clear the parked-threads map too
  // (switchThread now parks per-thread state) so a prior test's bucket can't be
  // unparked when this test's hook mounts and re-selects that agent id.
  useTimelineStore.getState().switchThread(0, null, false);
  useTimelineStore.setState({ threads: new Map() });
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
    expect(api.getTimeline).toHaveBeenCalledWith(42);
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
    expect(api.getTimeline).toHaveBeenLastCalledWith(42, { before: "5.0", limit: 50 });
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
    });
  });

  it("agentId=null sends no request and does not invoke switchThread (early return)", () => {
    // cover use-timeline.ts `if (agentId == null) return;` early-return guard.
    // Asserting that api is not called is not enough (enabled=false
    // already blocks); also pin that the effect does not enter the
    // switchThread path (otherwise activeThreadId would be set to null,
    // contaminating outer store state).
    const showError = vi.fn();
    const storeState = useTimelineStore.getState();
    const switchSpy = vi.spyOn(storeState, "switchThread");
    useTimelineStore.setState({
      switchThread: switchSpy,
    });

    renderHook(() => useTimeline(null, showError), { wrapper });

    expect(api.getTimeline).not.toHaveBeenCalled();
    expect(switchSpy).not.toHaveBeenCalled();
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
    expect(api.getTimeline).toHaveBeenLastCalledWith(2);
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

  it("hot-cache switch-back: switchThread restores synchronously, the object-identity skip does not re-fold the cached snapshot, and item 8 fires a background reconcile refetch", async () => {
    // Hot-cache round-trip: A → B → A. On switching back to A, switchThread
    // synchronously installs the cached items AND records the cached response
    // object in lastAppliedDataRef; the data effect fires in the same cycle with
    // that very object → early-returns (the object-identity skip), so the cached
    // snapshot is NOT re-folded via reloadSnapshot. Item 8 additionally fires a
    // background reconcile refetch on the switch-back (the !hadBucket guard is
    // gone). Here that refetch is made to HANG, so only the synchronous skip is
    // observable: the cached content shows while the reconcile is in flight.
    const showError = vi.fn();
    vi.mocked(api.getTimeline)
      .mockResolvedValueOnce(tlResp([snapshotItem({ payload: "A first fetch" })]))
      .mockResolvedValueOnce(tlResp([snapshotItem({ payload: "B first fetch" })]))
      // Switch-back reconcile refetch (item 8) — never settles.
      .mockImplementationOnce(
        () => new Promise<TimelineResponse>(() => { /* hang */ }),
      );

    const { result, rerender, unmount } = renderHook(
      ({ tid }: { tid: number | null }) => useTimeline(tid, showError),
      { initialProps: { tid: 1 }, wrapper },
    );

    // 1. First mount A — cold cache → fetch → data effect folds the response.
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("A first fetch"));
    expect(api.getTimeline).toHaveBeenCalledTimes(1);

    // 2. Switch to B — cold cache → fetch.
    rerender({ tid: 2 });
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("B first fetch"));
    expect(api.getTimeline).toHaveBeenCalledTimes(2);

    // 3. Spy switchThread + reloadSnapshot, then switch back to A.
    const storeState = useTimelineStore.getState();
    const switchSpy = vi.spyOn(storeState, "switchThread");
    const reloadSpy = vi.spyOn(storeState, "reloadSnapshot");
    useTimelineStore.setState({ switchThread: switchSpy, reloadSnapshot: reloadSpy });

    rerender({ tid: 1 });
    // switchThread is called synchronously in the agentId effect (cached branch).
    expect(switchSpy).toHaveBeenCalledTimes(1);
    expect(switchSpy.mock.calls[0][0]).toBe(1);
    expect(switchSpy.mock.calls[0][1]?.[0]?.payload).toBe("A first fetch");
    // Item 8: the switch-back fired a reconcile refetch (the third getTimeline).
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledTimes(3));

    // The object-identity skip means the cached snapshot itself is never
    // re-folded; the reconcile refetch is still hanging, so reloadSnapshot has
    // not run and A's cached items are still shown.
    await new Promise((r) => setTimeout(r, 0));
    expect(reloadSpy).not.toHaveBeenCalled();
    expect(result.current.items[0]?.payload).toBe("A first fetch");
    expect(result.current.items).toHaveLength(1);

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

  it("switched-away thread folds background SSE into its bucket; switch-back refetches to reconcile (item 8) and the merge keeps the in-flight bubble", async () => {
    // The store can receive an already-buffered thread A frame while B is shown;
    // it folds into A's parked bucket. Item 8 makes switch-back ALWAYS refetch to
    // reconcile (a live bucket is not a freshness guarantee — an event missed
    // during a socket gap leaves it silently stale). The merge must NOT clobber
    // live content: mergeSnapshotWithStreaming rule 3 keeps the single in-flight
    // streaming bubble (msg_idx === msg_count), so the reconcile is safe.
    const showError = vi.fn();
    vi.mocked(api.getTimeline)
      .mockResolvedValueOnce(
        tlResp([snapshotItem({ item_id: "1.0", kind: "agent_chat", payload: "A initial" })]),
      ) // thread 1 cold fetch (msg_count = 2)
      .mockResolvedValueOnce(tlResp([])) // thread 2 cold fetch
      // Switch-back reconcile refetch: the server snapshot still holds only the
      // committed 1.0 (msg_count 2), so the streaming 2.0 (msg_idx 2 === msg_count)
      // is preserved by the merge rather than dropped as stale.
      .mockResolvedValue(
        tlResp([snapshotItem({ item_id: "1.0", kind: "agent_chat", payload: "A initial" })]),
      );

    const { result, rerender, unmount } = renderHook(
      ({ tid }: { tid: number | null }) => useTimeline(tid, showError),
      { initialProps: { tid: 1 }, wrapper },
    );
    await waitFor(() => expect(result.current.items[0]?.payload).toBe("A initial"));

    // Switch to thread 2 — thread 1 is parked with its snapshot.
    rerender({ tid: 2 });
    await waitFor(() => expect(result.current.items).toEqual([]));

    // A buffered SSE event for switched-away thread 1 folds into its bucket,
    // not the active view (thread 2).
    pushEvent({ role: "chat_start", agent_id: 1, item_id: "2.0" });
    pushEvent({ role: "chat_delta", agent_id: 1, item_id: "2.0", content: "streamed while away" });
    expect(result.current.items).toEqual([]);

    // Switch back to thread 1 — the background bubble shows immediately from the
    // bucket, and item 8 fires exactly one reconcile refetch that keeps it.
    const callsBeforeReturn = vi.mocked(api.getTimeline).mock.calls.length;
    rerender({ tid: 1 });
    await waitFor(() =>
      expect(result.current.items.find((i) => i.item_id === "2.0")?.payload).toBe(
        "streamed while away",
      ),
    );
    expect(result.current.items.map((i) => i.item_id)).toEqual(["1.0", "2.0"]);
    await waitFor(() =>
      expect(vi.mocked(api.getTimeline).mock.calls.length).toBe(callsBeforeReturn + 1),
    );
    // The reconcile did NOT clobber the in-flight bubble.
    expect(result.current.items.find((i) => i.item_id === "2.0")?.payload).toBe(
      "streamed while away",
    );
    unmount();
  });

  it("switching to a thread with a cached snapshot but NO live bucket forces a refetch (LRU-evicted freshness)", async () => {
    // An LRU-evicted thread loses its live bucket but keeps its React Query
    // snapshot (gcTime). With staleTime=5min the cached snapshot would not
    // auto-refetch and the guard would skip folding it → the thread would show
    // stale, background-missed state. The switch effect must force a refetch
    // when a cached snapshot seeds a no-bucket switch.
    const showError = vi.fn();
    // Pre-seed a stale cached snapshot for thread 7 (no live bucket exists).
    queryClient.setQueryData<TimelineResponse>(
      ["timeline", 7],
      tlResp([snapshotItem({ item_id: "1.0", payload: "stale cached" })]),
    );
    // The forced refetch returns the fresh post-background state.
    vi.mocked(api.getTimeline).mockResolvedValue(
      tlResp([
        snapshotItem({ item_id: "1.0", payload: "fresh" }),
        snapshotItem({ item_id: "2.0", payload: "happened while away" }),
      ]),
    );

    const { result, unmount } = renderHook(() => useTimeline(7, showError), { wrapper });

    // No bucket → the switch effect invalidates → refetch replaces the stale
    // cached seed with fresh data (rather than sitting on "stale cached").
    await waitFor(() =>
      expect(result.current.items.map((i) => i.payload)).toEqual(["fresh", "happened while away"]),
    );
    expect(api.getTimeline).toHaveBeenCalledWith(7);
    unmount();
  });

  it("compact_done for a NON-active thread does not refetch the active thread (targets ev.agent_id)", async () => {
    // A buffered compact_done for a non-active agent must NOT refetch the active
    // thread — else a just-parked thread's compaction
    // spuriously refetches whatever you're viewing.
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp([]));
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledTimes(1)); // mount fetch

    pushEvent({ role: "compact_done", agent_id: 99 }); // a different (parked) thread compacts

    // The active thread (42) is NOT refetched — only its mount fetch happened.
    await new Promise((r) => setTimeout(r, 0));
    expect(api.getTimeline).toHaveBeenCalledTimes(1);
  });

  it("compact_done defers the refetch to the first NON-EMPTY post-compact snapshot (avoids caching the lagging pre-compact checkpoint)", async () => {
    // Regression for "compact 完成后 context UI 不立即刷新": invalidating on
    // compact_done itself reads the checkpoint BEFORE the compact commits
    // (a beat later locally, seconds on remote machines) and caches the
    // lagging pre-compact snapshot; a later switch-back then seeds it and
    // keep-all merging resurrects the old bubbles. The refetch must wait for
    // the first post-compact snapshot — by then the checkpoint is committed.
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp([]));
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
    // inbound_committed not received yet; should not trigger reload
    expect(api.getTimeline).toHaveBeenCalledTimes(1);
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

  it("poll invalidates the active timeline snapshot", async () => {
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    renderHook(() => useTimeline(42, vi.fn()), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalledWith(42));
    invalidateSpy.mockClear();

    pushConnectionEvent({ type: "poll" });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["timeline", 42] });
  });

  it("poll with no active agent does not invalidate a timeline snapshot", () => {
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    renderHook(() => useTimeline(null, vi.fn()), { wrapper });

    pushConnectionEvent({ type: "poll" });

    expect(invalidateSpy).not.toHaveBeenCalled();
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
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp([]));

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
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp([]));

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
      vi.mocked(api.getTimeline).mockResolvedValue(tlResp([]));
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
      vi.mocked(api.getTimeline).mockResolvedValue(tlResp([]));
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
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp([]));
    renderHook(() => useTimeline(42, showError), { wrapper });
    await waitFor(() => expect(api.getTimeline).toHaveBeenCalled());

    startTransitionSpy.mockClear();
    pushEvent({ role: "exec_start", item_id: "5.0", agent_id: 42 });

    expect(startTransitionSpy).not.toHaveBeenCalled();
  });

  it("if(isStreamingDelta) → false mutation: streaming events miss transition (kills L172 false)", async () => {
    const showError = vi.fn();
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp([]));
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
    vi.mocked(api.getTimeline).mockResolvedValue(tlResp([]));
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
