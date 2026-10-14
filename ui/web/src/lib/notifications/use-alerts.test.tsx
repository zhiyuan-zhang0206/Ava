// SSE notifications reconcile authoritative snapshots, including capped pages.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider, useAuth } from "@/lib/auth/auth-context";
import { AlertsProvider, useAlerts, useAlertsSection } from "@/lib/notifications/use-alerts";
import type { Alert, AlertsResponse } from "@/lib/contracts/types";

const mocks = vi.hoisted(() => ({ getAlerts: vi.fn(), checkAuth: vi.fn() }));
vi.mock("@/lib/transport/api", () => ({
  API_BASE: "",
  api: { getAlerts: mocks.getAlerts, checkAuth: mocks.checkAuth },
}));

function row(overrides: Partial<Alert> = {}): Alert {
  return {
    id: 1,
    status: "unresolved",
    severity: "error",
    alertname: "r",
    labels: {},
    annotations: {},
    starts_at: "2026-08-12T20:00:00Z",
    ends_at: null,
    fingerprint: "f",
    generator_url: "",
    source: "grafana",
    notified_at: null,
    created_at: "2026-08-12T20:00:00Z",
    updated_at: "2026-08-12T20:00:00Z",
    ...overrides,
  };
}

function resp(alerts: Alert[], unresolved: number): AlertsResponse {
  return {
    alerts,
    meta: { window: "24h", total: alerts.length, unresolved_count: unresolved },
  };
}

describe("useAlertsSection", () => {
  it("pins the section query to the 24h window named by its empty state", async () => {
    mocks.getAlerts.mockResolvedValue(resp([], 0));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useAlertsSection(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mocks.getAlerts).toHaveBeenCalledWith({
      window: "24h",
      limit: 200,
    });
    queryClient.clear();
  });
});

describe("useAlerts", () => {
  it("fetches the default snapshot when the SSE provider has not warmed the cache", async () => {
    mocks.getAlerts.mockResolvedValue(resp([], 0));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useAlerts(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mocks.getAlerts).toHaveBeenCalledWith({});
    queryClient.clear();
  });
});

class MockEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  url: string;
  init?: EventSourceInit;
  readyState = MockEventSource.CONNECTING;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;

  constructor(url: string, init?: EventSourceInit) {
    this.url = url;
    this.init = init;
    // eslint-disable-next-line @typescript-eslint/no-this-alias -- mock must track the latest constructed instance
    lastInstance = this;
  }

  close(): void {
    this.readyState = MockEventSource.CLOSED;
  }

  fireErrorWithReadyState(state: number): void {
    this.readyState = state;
    this.onerror?.(new Event("error"));
  }

  fireOpen(): void {
    this.readyState = MockEventSource.OPEN;
    this.onopen?.(new Event("open"));
  }

  fireMessage(data: string): void {
    this.onmessage?.(new MessageEvent("message", { data }));
  }
}

let lastInstance: MockEventSource | null = null;
let queryClients: QueryClient[] = [];

function deferredAuthResult() {
  let resolve!: (result: { authenticated: boolean }) => void;
  const promise = new Promise<{ authenticated: boolean }>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function alertsWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  queryClients.push(queryClient);
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <AlertsProvider>{children}</AlertsProvider>
        </AuthProvider>
      </QueryClientProvider>
    );
  };
}

function expectInstance(): MockEventSource {
  if (!lastInstance) throw new Error("EventSource was not constructed");
  return lastInstance;
}

async function waitForInstance(): Promise<void> {
  await waitFor(() => expect(lastInstance).not.toBeNull());
}

describe("AlertsProvider connection auth gating", () => {
  beforeEach(() => {
    lastInstance = null;
    queryClients = [];
    mocks.getAlerts.mockReset();
    mocks.getAlerts.mockResolvedValue(resp([], 0));
    mocks.checkAuth.mockReset();
    mocks.checkAuth.mockResolvedValue({ authenticated: true });
    vi.stubGlobal("EventSource", MockEventSource);
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
  });

  afterEach(() => {
    cleanup();
    for (const queryClient of queryClients) queryClient.clear();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("unauthenticated → no EventSource is constructed for /api/alerts/stream", async () => {
    mocks.checkAuth.mockResolvedValue({ authenticated: false });

    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("unauthenticated"));
    expect(lastInstance).toBeNull();
  });

  it("reconnect repairs active alert readers and marks inactive caches stale", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForInstance();
    const queryClient = queryClients.at(-1);
    if (!queryClient) throw new Error("QueryClient was not constructed");
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    act(() => {
      expectInstance().onopen?.(new Event("open"));
    });

    expect(invalidateSpy).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["alerts"],
    });
    expect(invalidateSpy).not.toHaveBeenCalledWith({
      queryKey: ["alerts", "section"],
    });
  });

  it("repeated frames outside the page cannot inflate the uncapped count", async () => {
    mocks.getAlerts.mockResolvedValue(resp([row({ id: 2 })], 13));
    const { result } = renderHook(() => useAlerts(), { wrapper: alertsWrapper() });
    await waitForInstance();
    await waitFor(() => expect(result.current.data?.meta.unresolved_count).toBe(13));
    const reads = mocks.getAlerts.mock.calls.length;
    act(() => expectInstance().fireMessage(JSON.stringify(row({ id: 999 }))));
    await waitFor(() => expect(mocks.getAlerts.mock.calls.length).toBeGreaterThan(reads));
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(result.current.data?.meta.unresolved_count).toBe(13);
    expect(result.current.data?.alerts.map((a) => a.id)).toEqual([2]);

    const nextReads = mocks.getAlerts.mock.calls.length;
    act(() => expectInstance().fireMessage(JSON.stringify(row({ id: 999 }))));
    await waitFor(() => expect(mocks.getAlerts.mock.calls.length).toBeGreaterThan(nextReads));
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(result.current.data?.meta.unresolved_count).toBe(13);
  });

  it("a resolution outside the page updates counts and preserves server ordering", async () => {
    mocks.getAlerts.mockResolvedValue(resp([row({ id: 2 })], 13));
    const { result } = renderHook(() => useAlerts(), { wrapper: alertsWrapper() });
    await waitForInstance();
    await waitFor(() => expect(result.current.data?.meta.unresolved_count).toBe(13));
    mocks.getAlerts.mockResolvedValue(resp([row({ id: 2 }), row({ id: 3, status: "resolved" })], 12));
    act(() => expectInstance().fireMessage(JSON.stringify(row({ id: 999, status: "resolved" }))));
    await waitFor(() => expect(result.current.data?.meta.unresolved_count).toBe(12));
    expect(result.current.data?.alerts.map((a) => a.id)).toEqual([2, 3]);
  });

  it("heartbeats and malformed frames do not fetch alert snapshots", async () => {
    const { result } = renderHook(() => useAlerts(), { wrapper: alertsWrapper() });
    await waitForInstance();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const reads = mocks.getAlerts.mock.calls.length;
    act(() => {
      expectInstance().fireMessage(JSON.stringify({ role: "heartbeat" }));
      expectInstance().fireMessage("bad-json");
      expectInstance().fireMessage(JSON.stringify({ id: "invalid" }));
    });
    expect(mocks.getAlerts).toHaveBeenCalledTimes(reads);
  });

  it("CLOSED with an invalid session → auth becomes unauthenticated and never reopens", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForInstance();
    const first = expectInstance();
    vi.useFakeTimers();
    mocks.checkAuth.mockResolvedValue({ authenticated: false });
    act(() => first.fireErrorWithReadyState(MockEventSource.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });

    vi.useRealTimers();
    await waitFor(() => expect(result.current).toBe("unauthenticated"));
    vi.useFakeTimers();
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(expectInstance()).toBe(first);
  });

  it("CLOSED with a valid session → reopens at the base delay", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForInstance();
    const first = expectInstance();
    vi.useFakeTimers();
    act(() => first.fireErrorWithReadyState(MockEventSource.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(999);
    });
    expect(expectInstance()).toBe(first);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(expectInstance()).not.toBe(first);
  });

  it("CLOSED when the session probe rejects → reopens at the base delay", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForInstance();
    const first = expectInstance();
    vi.useFakeTimers();
    mocks.checkAuth.mockRejectedValue(new Error("gateway unreachable"));
    act(() => first.fireErrorWithReadyState(MockEventSource.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(999);
    });
    expect(expectInstance()).toBe(first);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(expectInstance()).not.toBe(first);
  });

  it("a late invalid CLOSED probe cannot invalidate a replacement provider", async () => {
    const staleProbe = deferredAuthResult();
    mocks.checkAuth
      .mockResolvedValueOnce({ authenticated: true })
      .mockReturnValueOnce(staleProbe.promise)
      .mockResolvedValueOnce({ authenticated: true });

    const firstMount = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });
    await waitFor(() => expect(firstMount.result.current).toBe("authenticated"));
    await waitForInstance();
    act(() => expectInstance().fireErrorWithReadyState(MockEventSource.CLOSED));
    firstMount.unmount();

    lastInstance = null;
    const replacement = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });
    await waitFor(() => expect(replacement.result.current).toBe("authenticated"));
    await waitForInstance();
    const replacementSource = expectInstance();

    await act(async () => {
      staleProbe.resolve({ authenticated: false });
      await staleProbe.promise;
    });

    expect(replacement.result.current).toBe("authenticated");
    expect(expectInstance()).toBe(replacementSource);
    expect(replacementSource.readyState).not.toBe(MockEventSource.CLOSED);
  });

  it("consecutive valid-session CLOSED failures use 1s then 2s backoff", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForInstance();
    vi.useFakeTimers();
    const first = expectInstance();

    act(() => first.fireErrorWithReadyState(MockEventSource.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    const second = expectInstance();
    expect(second).not.toBe(first);

    act(() => second.fireErrorWithReadyState(MockEventSource.CLOSED));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      vi.advanceTimersByTime(1_999);
    });
    expect(expectInstance()).toBe(second);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(expectInstance()).not.toBe(second);
  });

  it("a heartbeat moves the 45s watchdog deadline", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });
    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForInstance();
    const first = expectInstance();

    vi.useFakeTimers();
    act(() => {
      first.fireOpen();
      vi.advanceTimersByTime(30_000);
      first.fireMessage(JSON.stringify({ role: "heartbeat" }));
      vi.advanceTimersByTime(44_999);
    });
    expect(expectInstance()).toBe(first);
    act(() => { vi.advanceTimersByTime(1); });
    expect(expectInstance()).not.toBe(first);
  });

  it("three malformed frames schedule a retry without closing the old source", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });
    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForInstance();
    const first = expectInstance();

    vi.useFakeTimers();
    act(() => {
      first.fireOpen();
      first.fireMessage("{");
      first.fireMessage("{");
      first.fireMessage("{");
    });
    expect(first.readyState).toBe(MockEventSource.OPEN);
    act(() => { vi.advanceTimersByTime(999); });
    expect(expectInstance()).toBe(first);
    act(() => { vi.advanceTimersByTime(1); });
    expect(expectInstance()).not.toBe(first);
    expect(first.readyState).toBe(MockEventSource.CLOSED);
  });

  it("unmount cancels a pending CLOSED retry", async () => {
    const mounted = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });
    await waitFor(() => expect(mounted.result.current).toBe("authenticated"));
    await waitForInstance();
    const first = expectInstance();

    vi.useFakeTimers();
    act(() => first.fireErrorWithReadyState(MockEventSource.CLOSED));
    await act(async () => await Promise.resolve());
    mounted.unmount();
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(expectInstance()).toBe(first);
  });
});

describe("AlertsProvider hidden-tab gating", () => {
  beforeEach(() => {
    lastInstance = null;
    queryClients = [];
    mocks.getAlerts.mockReset();
    mocks.getAlerts.mockResolvedValue(resp([], 0));
    mocks.checkAuth.mockReset();
    mocks.checkAuth.mockResolvedValue({ authenticated: true });
    vi.stubGlobal("EventSource", MockEventSource);
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
  });

  afterEach(() => {
    // Leave the shared document visible for the next test.
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    cleanup();
    for (const queryClient of queryClients) queryClient.clear();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("hidden page closes the stream without polling; visible reopens", async () => {
    const { result } = renderHook(() => useAuth().status, { wrapper: alertsWrapper() });

    await waitFor(() => expect(result.current).toBe("authenticated"));
    await waitForInstance();
    const first = expectInstance();
    const queryClient = queryClients[0];
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    try {
      vi.useFakeTimers();
      act(() => {
        Object.defineProperty(document, "visibilityState", {
          configurable: true,
          value: "hidden",
        });
        document.dispatchEvent(new Event("visibilitychange"));
      });

      // The stream effect closes its EventSource (freeing the connection slot).
      expect(first.readyState).toBe(MockEventSource.CLOSED);

      invalidateSpy.mockClear();
      act(() => {
        vi.advanceTimersByTime(7_000);
      });
      // Hidden views do no periodic reads.
      expect(invalidateSpy).not.toHaveBeenCalled();

      invalidateSpy.mockClear();
      act(() => {
        Object.defineProperty(document, "visibilityState", {
          configurable: true,
          value: "visible",
        });
        document.dispatchEvent(new Event("visibilitychange"));
      });

      // Returning visible opens a fresh stream and stops the poll.
      expect(expectInstance()).not.toBe(first);
      expect(expectInstance().url).toContain("/api/alerts/stream");
      act(() => {
        vi.advanceTimersByTime(7_000);
      });
      expect(invalidateSpy).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
