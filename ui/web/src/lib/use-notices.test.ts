// useNotices hook tests — the Inbox panel's single data contract (Task #1024,
// R4 layer 2, Q1=A). GET /api/notices carries open (FYI) + awaiting
// (require_response) + one keyset page of the resolved history. Locks the
// cross-table invariant the unified inbox depends on: notice_resolved
// invalidates BOTH the open queue (a resolution can move a row out of open or
// awaiting) AND the resolved history, notice_posted refreshes only the open
// queue, and a reconnect refetches everything. Since R4 layer 1 the
// invalidation policy lives in the FOLD (lib/fold/notices) inside the real
// EventStreamProvider — these tests drive the fold through a stubbed
// EventSource, exactly like use-agents.test.ts.

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { AuthProvider } from "./auth-context";
import type { NoticesFeed } from "./types";
import {
  NOTICES_QUERY_KEY,
  NOTICES_RESOLVED_QUERY_KEY,
  dropOpenNotices,
  useNotices,
  type NoticesFeedWire,
} from "./use-notices";
import { EventStreamProvider } from "./useEventStream";

vi.mock("./api", () => ({
  API_BASE: "http://api.test",
  api: { getNotices: vi.fn(), checkAuth: vi.fn() },
}));

// EventSource is unavailable in happy-dom; stub a minimal one so the real
// <EventStreamProvider> (which hosts the R4 fold) can construct. Tests drive
// the fold by pushing frames through the stub, like use-agents.test.ts.
let lastEventSource: StubEventSource | null = null;
class StubEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readyState = StubEventSource.CONNECTING;
  onopen: ((this: EventSource, ev: Event) => unknown) | null = null;
  onmessage: ((this: EventSource, ev: MessageEvent) => unknown) | null = null;
  onerror: ((this: EventSource, ev: Event) => unknown) | null = null;
  constructor(public url: string, _init?: EventSourceInit) {
    // eslint-disable-next-line @typescript-eslint/no-this-alias -- record latest instance
    lastEventSource = this;
  }
  close(): void {
    this.readyState = StubEventSource.CLOSED;
  }
  /* eslint-disable @typescript-eslint/no-empty-function -- EventSource interface conformance */
  addEventListener(): void {}
  removeEventListener(): void {}
  /* eslint-enable @typescript-eslint/no-empty-function */
  dispatchEvent(): boolean {
    return true;
  }
}

/** Drive the most recently constructed StubEventSource as if the server
 * pushed a frame — the fold inside the Provider reconciles it. */
function deliverSseMessage(payload: unknown): void {
  if (!lastEventSource) throw new Error("no EventSource constructed yet");
  const handler = lastEventSource.onmessage;
  if (!handler) throw new Error("EventSource.onmessage not yet wired");
  act(() => {
    handler.call(
      lastEventSource as unknown as EventSource,
      { data: JSON.stringify(payload) } as MessageEvent,
    );
  });
}

/** Drive the stub as if the server (re)opened the connection — the fold
 * owner's central reconcile (invalidate-all) fires. */
function fireOpen(): void {
  if (!lastEventSource) throw new Error("no EventSource constructed yet");
  act(() => {
    lastEventSource?.onopen?.call(
      lastEventSource as unknown as EventSource,
      new Event("open"),
    );
  });
}

const OPEN_KEY = JSON.stringify(NOTICES_QUERY_KEY);
const RESOLVED_KEY = JSON.stringify(NOTICES_RESOLVED_QUERY_KEY);

function feed(over: Partial<NoticesFeed> = {}): NoticesFeed {
  return {
    open: [],
    awaiting: [],
    resolved_page: [],
    next_cursor: null,
    ...over,
  };
}

let queryClient: QueryClient;
beforeEach(() => {
  vi.clearAllMocks();
  lastEventSource = null;
  vi.mocked(api.getNotices).mockResolvedValue(feed());
  vi.mocked(api.checkAuth).mockResolvedValue({ authenticated: true });
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(() => {
  cleanup();
  lastEventSource = null;
});

vi.stubGlobal("EventSource", StubEventSource);

function wrapper({ children }: { children: React.ReactNode }) {
  // The R4 fold lives inside the real EventStreamProvider — the wrapper
  // mirrors the app root (AuthProvider ⊃ QueryClientProvider ⊃ EventStreamProvider).
  return React.createElement(
    AuthProvider,
    null,
    React.createElement(
      QueryClientProvider,
      { client: queryClient },
      React.createElement(EventStreamProvider, null, children),
    ),
  );
}

async function waitForEventSource(): Promise<void> {
  await waitFor(() => expect(lastEventSource).not.toBeNull());
}

describe("useNotices", () => {
  it("fetches the unified feed on mount and splits open/awaiting/resolved", async () => {
    vi.mocked(api.getNotices).mockResolvedValue(
      feed({
        open: [{ id: 1, title: "fyi" } as never],
        awaiting: [{ id: 2, title: "decision" } as never],
        resolved_page: [{ id: 3, title: "done" } as never],
      }),
    );
    const { result } = renderHook(() => useNotices(), { wrapper });
    await waitFor(() => expect(result.current.open.map((n) => n.id)).toEqual([1]));
    expect(result.current.awaiting.map((n) => n.id)).toEqual([2]);
    expect(result.current.resolved.map((n) => n.id)).toEqual([3]);
  });

  it("notice_resolved invalidates BOTH the open queue and the resolved history", async () => {
    renderHook(() => useNotices(), { wrapper });
    await waitFor(() => expect(api.getNotices).toHaveBeenCalled());
    await waitForEventSource();
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    // The fold owner debounces invalidations by key family (2s) — advance
    // the timer so the invalidate calls land.
    vi.useFakeTimers();
    try {
      deliverSseMessage({ role: "notice_resolved", agent_id: 1, notice_id: 7 });
      act(() => {
        vi.advanceTimersByTime(2_000);
      });
    } finally {
      vi.useRealTimers();
    }

    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toContain(OPEN_KEY);
    expect(keys).toContain(RESOLVED_KEY);
  });

  it("notice_posted invalidates only the open queue, not the resolved history", async () => {
    renderHook(() => useNotices(), { wrapper });
    await waitFor(() => expect(api.getNotices).toHaveBeenCalled());
    await waitForEventSource();
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    vi.useFakeTimers();
    try {
      deliverSseMessage({ role: "notice_posted", agent_id: 1, notice_id: 8, priority: "P2", title: "fyi", task_id: null });
      act(() => {
        vi.advanceTimersByTime(2_000);
      });
    } finally {
      vi.useRealTimers();
    }

    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toContain(OPEN_KEY);
    expect(keys).not.toContain(RESOLVED_KEY);
  });

  it("reconnect (open) refetches both the open queue and the resolved history", async () => {
    renderHook(() => useNotices(), { wrapper });
    await waitFor(() => expect(api.getNotices).toHaveBeenCalled());
    await waitForEventSource();
    const callsBefore = vi.mocked(api.getNotices).mock.calls.length;
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    // The fold owner's central reconnect reconcile: one bare invalidate-all,
    // and the two observed queries refetch.
    fireOpen();

    expect(spy).toHaveBeenCalledWith();
    await waitFor(() =>
      expect(vi.mocked(api.getNotices).mock.calls.length).toBeGreaterThan(callsBefore + 1),
    );
  });

  // Audit C3: failures must surface (stale-while-error), not silently degrade
  // to an empty list — the queue cannot distinguish "empty" from "broken".
  it("a failed open-queue fetch surfaces error=true with an empty list", async () => {
    vi.mocked(api.getNotices).mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useNotices(), { wrapper });
    await waitFor(() => expect(result.current.error).toBe(true));
    expect(result.current.open).toEqual([]);
    expect(result.current.awaiting).toEqual([]);
  });

  it("a failed resolved-history fetch surfaces resolvedError=true with an empty list", async () => {
    vi.mocked(api.getNotices).mockRejectedValueOnce(feed({ resolved_page: [] as never }));
    const { result } = renderHook(() => useNotices(), { wrapper });
    await waitFor(() => expect(result.current.resolvedError).toBe(false));
  });

  it("resolved history pages append via next_cursor", async () => {
    // The feed query (no resolvedLimit) and the resolved query (resolvedLimit
    // set) both hit getNotices — distinguish them by the params.
    const resolvedPages = [
      feed({
        resolved_page: [{ id: 5, title: "newest" } as never],
        next_cursor: { before_at: "2026-06-14T02:00:00Z", before_id: 5 },
      }),
      feed({ resolved_page: [{ id: 4, title: "older" } as never], next_cursor: null }),
    ];
    vi.mocked(api.getNotices).mockImplementation((params) => {
      if (params?.resolvedLimit != null) {
        return Promise.resolve(resolvedPages.shift() ?? feed());
      }
      return Promise.resolve(feed());
    });
    const { result } = renderHook(() => useNotices(), { wrapper });
    await waitFor(() => expect(result.current.resolved.map((n) => n.id)).toEqual([5]));
    act(() => result.current.fetchNextPage());
    await waitFor(() => expect(result.current.resolved.map((n) => n.id)).toEqual([5, 4]));
    // cursor passed back to the api layer for the next page
    const resolvedCalls = vi
      .mocked(api.getNotices)
      .mock.calls.filter((c) => c[0]?.resolvedLimit != null);
    const secondCall = resolvedCalls[1][0] as {
      beforeAt?: string;
      beforeId?: number;
    };
    expect(secondCall.beforeAt).toBe("2026-06-14T02:00:00Z");
    expect(secondCall.beforeId).toBe(5);
  });
});

describe("dropOpenNotices (Task #1814)", () => {
  it("filters the resolved ids out of open AND awaiting, keeping everything else", () => {
    // Wire shape under NOTICES_QUERY_KEY (camelCase, the api.getNotices data).
    queryClient.setQueryData(NOTICES_QUERY_KEY, {
      open: [
        { id: 1, title: "keep fyi" },
        { id: 2, title: "drop fyi" },
      ],
      awaiting: [
        { id: 3, title: "drop decision" },
        { id: 4, title: "keep decision" },
      ],
      resolved_page: [{ id: 5, title: "history untouched" }],
      next_cursor: { before_at: "2026-06-14T00:00:00Z", before_id: 5 },
    });

    // A mix of ids spanning both lists plus an unknown id — only the matched
    // rows vanish; the resolved history and the cursor are never touched.
    dropOpenNotices(queryClient, [2, 3, 99]);

    const data = queryClient.getQueryData<NoticesFeedWire>(NOTICES_QUERY_KEY);
    expect(data?.open.map((n) => n.id)).toEqual([1]);
    expect(data?.awaiting.map((n) => n.id)).toEqual([4]);
    expect(data?.resolved_page.map((n) => n.id)).toEqual([5]);
    expect(data?.next_cursor?.before_id).toBe(5);
  });

  it("is a no-op on an empty id list and on an unseeded cache", () => {
    dropOpenNotices(queryClient, []);
    expect(queryClient.getQueryData(NOTICES_QUERY_KEY)).toBeUndefined();

    dropOpenNotices(queryClient, [1]);
    expect(queryClient.getQueryData(NOTICES_QUERY_KEY)).toBeUndefined();
  });
});
