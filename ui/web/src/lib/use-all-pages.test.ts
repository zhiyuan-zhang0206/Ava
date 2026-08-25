// useAllPages hook tests — the fleet-wide open-pages cache (every agent's live
// page in one fetch), SSE-driven over TanStack Query. Mirrors use-agent-pages
// but without the per-agent filter: page_opened/page_closed for ANY agent fold
// in, keyed by (agent_id, name).

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { AuthProvider } from "./auth-context";
import type { PageRow, SystemEvent } from "./types";
import { useAllPages } from "./use-all-pages";
import { EventStreamProvider } from "./useEventStream";

vi.mock("./api", () => ({
  API_BASE: "http://api.test",
  api: { listAllPages: vi.fn(), checkAuth: vi.fn() },
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
function pageRow(overrides: Partial<PageRow> & { name: string; agent_id: number }): PageRow {
  return {
    id: ++_pid,
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
  agent_id: number;
  port?: number;
  url?: string;
}): SystemEvent {
  return {
    role: "page_opened",
    agent_id: over.agent_id,
    page_id: ++_pid,
    name: over.name,
    port: over.port ?? 9100,
    title: over.name,
    url: over.url ?? `http://host/${over.name}`,
  };
}

let queryClient: QueryClient;
beforeEach(() => {
  vi.clearAllMocks();
  lastEventSource = null;
  vi.mocked(api.listAllPages).mockResolvedValue([]);
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

async function waitForFetch(): Promise<void> {
  await waitFor(() => expect(queryClient.getQueryData(["all-pages"])).toBeDefined());
}

describe("useAllPages", () => {
  it("fetches every agent's open pages on mount", async () => {
    vi.mocked(api.listAllPages).mockResolvedValue([
      pageRow({ name: "a", agent_id: 1 }),
      pageRow({ name: "b", agent_id: 2 }),
    ]);
    const { result } = renderHook(() => useAllPages(), { wrapper });
    await waitFor(() =>
      expect(result.current.map((p) => `${p.agent_id}:${p.name}`)).toEqual(["1:a", "2:b"]),
    );
    expect(api.listAllPages).toHaveBeenCalledTimes(1);
  });

  it("page_opened for any agent folds in without a refetch", async () => {
    vi.mocked(api.listAllPages).mockResolvedValue([pageRow({ name: "a", agent_id: 1 })]);
    const { result } = renderHook(() => useAllPages(), { wrapper });
    await waitFor(() => expect(result.current).toHaveLength(1));
    await waitForEventSource();

    deliverSseMessage(pageOpened({ name: "b", agent_id: 2 }));
    await waitFor(() =>
      expect(result.current.map((p) => `${p.agent_id}:${p.name}`)).toEqual(["1:a", "2:b"]),
    );
    expect(api.listAllPages).toHaveBeenCalledTimes(1); // SSE fold, not a refetch
  });

  it("replace-by-(agent,name): same agent + name updates in place", async () => {
    vi.mocked(api.listAllPages).mockResolvedValue([
      pageRow({ name: "dash", agent_id: 1, port: 9000 }),
    ]);
    const { result } = renderHook(() => useAllPages(), { wrapper });
    await waitFor(() => expect(result.current).toHaveLength(1));
    await waitForEventSource();

    deliverSseMessage(pageOpened({ name: "dash", agent_id: 1, port: 9999, url: "http://host/dash-new" }));
    await waitFor(() => expect(result.current[0].port).toBe(9999));
    expect(result.current).toHaveLength(1); // replaced, not duplicated
    expect(result.current[0].url).toBe("http://host/dash-new");
  });

  it("same name under a DIFFERENT agent is a distinct row (keyed by agent+name)", async () => {
    vi.mocked(api.listAllPages).mockResolvedValue([pageRow({ name: "dash", agent_id: 1 })]);
    const { result } = renderHook(() => useAllPages(), { wrapper });
    await waitFor(() => expect(result.current).toHaveLength(1));
    await waitForEventSource();

    deliverSseMessage(pageOpened({ name: "dash", agent_id: 2 }));
    await waitFor(() => expect(result.current).toHaveLength(2));
    expect(result.current.map((p) => p.agent_id).sort()).toEqual([1, 2]);
  });

  it("page_closed removes only the matching (agent, name)", async () => {
    vi.mocked(api.listAllPages).mockResolvedValue([
      pageRow({ name: "dash", agent_id: 1 }),
      pageRow({ name: "dash", agent_id: 2 }),
    ]);
    const { result } = renderHook(() => useAllPages(), { wrapper });
    await waitFor(() => expect(result.current).toHaveLength(2));
    await waitForEventSource();

    deliverSseMessage({ role: "page_closed", agent_id: 1, name: "dash" });
    await waitFor(() => expect(result.current.map((p) => p.agent_id)).toEqual([2]));
  });

  it("empty-cache guard: page_opened before the fetch lands is not seeded", async () => {
    let resolveFetch: (rows: PageRow[]) => void = () => undefined;
    vi.mocked(api.listAllPages).mockImplementation(
      () => new Promise((r) => { resolveFetch = r; }),
    );
    const { result } = renderHook(() => useAllPages(), { wrapper });
    await waitForEventSource();

    deliverSseMessage(pageOpened({ name: "early", agent_id: 1 }));
    expect(queryClient.getQueryData(["all-pages"])).toBeUndefined();
    expect(result.current).toEqual([]);

    act(() => resolveFetch([pageRow({ name: "from-fetch", agent_id: 1 })]));
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["from-fetch"]));
  });

  it("reconnect (open) refetches to reconcile events missed during the gap", async () => {
    vi.mocked(api.listAllPages).mockResolvedValueOnce([]);
    const { result } = renderHook(() => useAllPages(), { wrapper });
    await waitForFetch();
    await waitForEventSource();
    expect(api.listAllPages).toHaveBeenCalledTimes(1);

    vi.mocked(api.listAllPages).mockResolvedValueOnce([pageRow({ name: "missed", agent_id: 3 })]);
    fireOpen();
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["missed"]));
    expect(api.listAllPages).toHaveBeenCalledTimes(2);
  });
});
