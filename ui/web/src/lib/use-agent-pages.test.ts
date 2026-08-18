// useAgentPages hook tests — the inspector's open-pages list, SSE-driven over
// a TanStack Query cache (mirrors the agents-cache pattern). Covers the initial
// fetch, page_opened/page_closed folding, replace-by-name, the empty-cache
// guard (no partial seed before the fetch lands), cross-agent filtering, and
// reconnect reconciliation. Same mock strategy as use-timeline.test.ts.

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import type { PageRow, SystemEvent } from "./types";
import { useAgentPages } from "./use-agent-pages";
import { EventStreamProvider } from "./useEventStream";

vi.mock("./api", () => ({
  API_BASE: "http://api.test",
  api: { listPages: vi.fn() },
}));

// The R4 fold (layer 1) lives inside the real <EventStreamProvider>; tests
// drive it through a stubbed EventSource (same pattern as use-agents.test.ts).
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

function fireOpen(): void {
  if (!lastEventSource) throw new Error("no EventSource constructed yet");
  act(() => {
    lastEventSource?.onopen?.call(
      lastEventSource as unknown as EventSource,
      new Event("open"),
    );
  });
}

let _pid = 0;
function pageRow(overrides: Partial<PageRow> & { name: string }): PageRow {
  return {
    id: ++_pid,
    agent_id: 1,
    port: 9000 + _pid,
    title: overrides.name,
    serve_dir: null,
    url: `http://host/${overrides.name}`,
    created_at: "2026-01-01T00:00:00Z",
    closed_at: null,
    ...overrides,
  };
}

function pageOpened(over: {
  name: string;
  agent_id?: number;
  port?: number;
  title?: string | null;
  url?: string;
}): SystemEvent {
  return {
    role: "page_opened",
    agent_id: over.agent_id ?? 1,
    page_id: ++_pid,
    name: over.name,
    port: over.port ?? 9100,
    title: over.title ?? over.name,
    url: over.url ?? `http://host/${over.name}`,
  };
}

let queryClient: QueryClient;
beforeEach(() => {
  vi.clearAllMocks();
  lastEventSource = null;
  vi.mocked(api.listPages).mockResolvedValue([]);
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(() => {
  cleanup();
  lastEventSource = null;
});

vi.stubGlobal("EventSource", StubEventSource);

function wrapper({ children }: { children: React.ReactNode }) {
  // The R4 fold lives inside the real EventStreamProvider — the wrapper
  // mirrors the app root (QueryClientProvider ⊃ EventStreamProvider).
  return React.createElement(
    QueryClientProvider,
    { client: queryClient },
    React.createElement(EventStreamProvider, null, children),
  );
}

/** Wait until the initial fetch has landed in the cache (distinguishes a
 *  fetched empty list [] from the pre-fetch undefined — result.current is [] in
 *  both, so it can't gate the SSE push on its own). */
async function waitForFetch(): Promise<void> {
  await waitFor(() =>
    expect(queryClient.getQueryData(["agent-pages", 1])).toBeDefined(),
  );
}

describe("useAgentPages", () => {
  it("fetches the initial page list on mount", async () => {
    vi.mocked(api.listPages).mockResolvedValue([pageRow({ name: "panel-a" })]);
    const { result } = renderHook(() => useAgentPages(1), { wrapper });
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["panel-a"]));
    expect(api.listPages).toHaveBeenCalledWith(1);
  });

  it("page_opened folds a new page into the cache (no refetch)", async () => {
    vi.mocked(api.listPages).mockResolvedValue([]);
    const { result } = renderHook(() => useAgentPages(1), { wrapper });
    await waitForFetch();

    deliverSseMessage(pageOpened({ name: "panel-a" }));
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["panel-a"]));
    expect(api.listPages).toHaveBeenCalledTimes(1); // SSE fold, not a refetch
  });

  it("page_opened for an existing name replaces the row (port/url changed)", async () => {
    vi.mocked(api.listPages).mockResolvedValue([pageRow({ name: "panel-a", port: 9000 })]);
    const { result } = renderHook(() => useAgentPages(1), { wrapper });
    await waitFor(() => expect(result.current).toHaveLength(1));

    deliverSseMessage(pageOpened({ name: "panel-a", port: 9999, url: "http://host/panel-a-new" }));
    await waitFor(() => expect(result.current[0].port).toBe(9999));
    expect(result.current).toHaveLength(1); // replaced, not duplicated
    expect(result.current[0].url).toBe("http://host/panel-a-new");
  });

  it("page_closed removes the page by name", async () => {
    vi.mocked(api.listPages).mockResolvedValue([
      pageRow({ name: "panel-a" }),
      pageRow({ name: "panel-b" }),
    ]);
    const { result } = renderHook(() => useAgentPages(1), { wrapper });
    await waitFor(() => expect(result.current).toHaveLength(2));

    deliverSseMessage({ role: "page_closed", agent_id: 1, name: "panel-a" });
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["panel-b"]));
  });

  it("empty-cache guard: page_opened before the initial fetch lands is not seeded", async () => {
    // Hang the fetch so the query has no data yet when the SSE arrives.
    let resolveFetch: (rows: PageRow[]) => void = () => undefined;
    vi.mocked(api.listPages).mockImplementation(
      () => new Promise((r) => { resolveFetch = r; }),
    );
    const { result } = renderHook(() => useAgentPages(1), { wrapper });

    deliverSseMessage(pageOpened({ name: "early" }));
    // Cache is still undefined (fetch pending) → the guard drops the partial.
    expect(queryClient.getQueryData(["agent-pages", 1])).toBeUndefined();
    expect(result.current).toEqual([]);

    // The fetch is the source of truth for the initial list.
    act(() => resolveFetch([pageRow({ name: "from-fetch" })]));
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["from-fetch"]));
  });

  it("first page opening into an already-fetched empty list ([]) still merges", async () => {
    vi.mocked(api.listPages).mockResolvedValue([]);
    const { result } = renderHook(() => useAgentPages(1), { wrapper });
    await waitForFetch(); // fetched, empty ([])

    deliverSseMessage(pageOpened({ name: "first" }));
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["first"]));
  });

  it("ignores events for other agents", async () => {
    vi.mocked(api.listPages).mockResolvedValue([]);
    const { result } = renderHook(() => useAgentPages(1), { wrapper });
    await waitForFetch();

    deliverSseMessage(pageOpened({ name: "other", agent_id: 2 }));
    // agent 2's page must not appear in agent 1's list.
    expect(result.current).toEqual([]);
    expect(queryClient.getQueryData(["agent-pages", 1])).toEqual([]);
  });

  it("reconnect (open) refetches to reconcile events missed during the gap", async () => {
    vi.mocked(api.listPages).mockResolvedValueOnce([]);
    const { result } = renderHook(() => useAgentPages(1), { wrapper });
    await waitForFetch();
    expect(api.listPages).toHaveBeenCalledTimes(1);

    // A page opened while the socket was down; the reconnect refetch returns it.
    vi.mocked(api.listPages).mockResolvedValueOnce([pageRow({ name: "missed" })]);
    fireOpen();
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["missed"]));
    expect(api.listPages).toHaveBeenCalledTimes(2);
  });
});
