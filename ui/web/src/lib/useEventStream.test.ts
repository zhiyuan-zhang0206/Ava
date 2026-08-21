// useEventStream unit tests — mock global EventSource and verify
// readyState tri-state dispatch + onmessage parse path directly. This
// is the only load-bearing new logic from PR #18 (silent-failure-hunter
// F7+F8 fixes); the entire useTimeline connectionState behavior
// depends on this file's onerror branches being correct, so they must
// be tested directly.
//
// 2026-05-06 refactor: useEventStream changed from (agentId, onSystem,
// onConn) to a Context Provider pattern (agentId on EventStreamProvider).
// Tests use renderHook's `wrapper` option to wrap each hook call in a
// Provider.

import { act, cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useStore } from "./store";
import { useFoldOwner } from "./fold/owner";
import type { AgentRow } from "./types";
import type { SystemEvent } from "./types";
import type { ConnectionEvent } from "./useEventStream";
import {
  AgentEventStreamProvider,
  EventStreamProvider,
  useAgentEventStream,
  useEventStream,
} from "./useEventStream";

// -- mock global EventSource ----------------------------------------

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

  // Test-driver helper — set readyState + trigger onerror directly, not via EventSource API
  fireOpen(): void {
    this.readyState = MockEventSource.OPEN;
    this.onopen?.(new Event("open"));
  }
  fireMessage(data: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: data as string }));
  }
  fireErrorWithReadyState(state: number): void {
    this.readyState = state;
    this.onerror?.(new Event("error"));
  }
}

let lastInstance: MockEventSource | null = null;

beforeEach(() => {
  lastInstance = null;
  vi.stubGlobal("EventSource", MockEventSource);
  // The Providers read reconnectNonce + activeId from the store and list them
  // in effect deps — reset both so each test starts from a known baseline.
  useStore.setState({ reconnectNonce: 0, activeId: null });
});

afterEach(() => {
  // Unmount so each connection's effect cleanup clears any pending backoff-retry
  // timer (the connect-failure retry below schedules real setTimeouts) before the
  // next test — otherwise a stray timer could construct an EventSource mid-test.
  cleanup();
  vi.unstubAllGlobals();
});

function expectInstance(): MockEventSource {
  if (!lastInstance) throw new Error("EventSource was not constructed");
  return lastInstance;
}

// Helps renderHook wrap a hook into the Provider. agentId is passed via the wrapper factory.
function withProvider() {
  // The R4 fold owner inside EventStreamProvider reads the query cache, so
  // the Provider must sit under a QueryClientProvider (mirrors providers.tsx).
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(
      QueryClientProvider,
      { client: qc },
      createElement(EventStreamProvider, { children }),
    );
  };
}

// Variant of withProvider that also hands back the QueryClient, so a test can
// seed and assert the fold's cache writes.
function withProviderAndClient() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  // Plain arrow function (not an object method) — destructuring it out of the
  // return value would otherwise trip @typescript-eslint/unbound-method.
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(
      QueryClientProvider,
      { client: qc },
      createElement(EventStreamProvider, { children }),
    );
  return { qc, wrapper };
}

// Minimal AgentRow for fold-cache assertions (shape mirrors fold/fold.test.ts).
const baseAgent: AgentRow = {
  agent_id: 1,
  label: "a",
  status: "running",
  last_active_at: "2026-05-10T00:00:00Z", last_inbound_at: "2026-05-10T00:00:00Z",
  spawner: "user",
  fork_source_agent_id: null,
  fork_source_checkpoint_id: null,
  pid: 100,
  spawned_at: "2026-05-10T00:00:00Z",
  started_at: "2026-05-10T00:00:01Z",
  machine: "test",
  notices_awaiting_response: [],
  unread_notice_count: 0,
  heartbeat_paused_until: null,
  liveness_state: "online",
  last_probe_at: null,
};

// -- tests ─────────────────────────────────────────────────────────────────

describe("EventStreamProvider connection lifecycle", () => {
  it("mount + open → subscriber receives onConnectionEvent({type:'open'})", () => {
    const onSystem = vi.fn();
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(onSystem, onConn), { wrapper: withProvider() });
    act(() => expectInstance().fireOpen());

    expect(onConn).toHaveBeenCalledWith({ type: "open" });
    expect(onSystem).not.toHaveBeenCalled();
  });

  it("provider always opens EventSource (global connection)", () => {
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    expect(lastInstance).not.toBeNull();
    expect(expectInstance().url).toContain("/api/system");
  });

  it("URL is /api/system (global broadcast)", () => {
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    expect(expectInstance().url).toContain("/api/system");
  });

  it("opens with withCredentials so the session cookie reaches the cross-origin gateway", () => {
    // Regression: the gateway requires auth and EventSource cannot send a
    // Bearer header, so a cross-origin SSE GET (:3000 -> :8000) must carry the
    // session cookie via withCredentials — otherwise it 401s and live updates
    // never connect ("SSE connection lost").
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    expect(expectInstance().init?.withCredentials).toBe(true);
  });

  it("calling useEventStream outside Provider → throws (fail-fast, not silent)", () => {
    // Suppress React error-boundary console.error noise
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined /* suppress console.error in error-boundary test */);
    expect(() => renderHook(() => useEventStream(vi.fn(), vi.fn()))).toThrow(
      /EventStreamProvider/,
    );
    errSpy.mockRestore();
  });
});

describe("EventStreamProvider onmessage", () => {
  it("valid JSON → onSystemEvent receives the parsed event", () => {
    const onSystem = vi.fn();
    const onConn = vi.fn();

    renderHook(() => useEventStream(onSystem, onConn), { wrapper: withProvider() });
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage(
        JSON.stringify({ role: "chat_delta", agent_id: 42, content: "hi" }),
      );
    });

    expect(onSystem).toHaveBeenCalledWith({
      role: "chat_delta",
      agent_id: 42,
      content: "hi",
    });
  });

  it("malformed JSON → onConnectionEvent({type:'parse-failed'}) (no more silent console.error)", () => {
    const onSystem = vi.fn();
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(onSystem, onConn), { wrapper: withProvider() });
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage("{not valid json");
    });

    expect(onSystem).not.toHaveBeenCalled();
    expect(onConn).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "parse-failed",
        raw: "{not valid json",
        // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment -- expect.any() returns any, required by Vitest API
        error: expect.any(SyntaxError),
      }),
    );
  });

  it("non-string e.data (Blob etc.) → 'parse-failed', does not let String() itself crash", () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage({ type: "Blob", size: 100 });
    });

    expect(onConn).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "parse-failed",
        raw: "[non-string SSE payload]",
      }),
    );
  });
});

describe("EventStreamProvider onerror three-way readyState dispatch", () => {
  it("readyState=CLOSED → onConnectionEvent({type:'closed'})", () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    act(() => expectInstance().fireErrorWithReadyState(MockEventSource.CLOSED));

    expect(onConn).toHaveBeenCalledWith({ type: "closed" });
  });

  it("readyState=CONNECTING → onConnectionEvent({type:'reconnecting'})", () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    act(() => expectInstance().fireErrorWithReadyState(MockEventSource.CONNECTING));

    expect(onConn).toHaveBeenCalledWith({ type: "reconnecting" });
  });

  it("readyState=OPEN → no notification (transient fault absorbed by the browser)", () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    act(() => {
      expectInstance().fireOpen();
      onConn.mockClear();
      expectInstance().fireErrorWithReadyState(MockEventSource.OPEN);
    });

    expect(onConn).not.toHaveBeenCalled();
  });

  it("unknown readyState → throws (fail-fast, no case _: catch-all)", () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    expect(() => {
      act(() => expectInstance().fireErrorWithReadyState(99));
    }).toThrow(/unknown EventSource readyState: 99/);
  });
});

describe("EventStreamProvider connect-failure backoff retry", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("dead-end CLOSED error schedules a backoff reopen (a fresh EventSource after the delay)", () => {
    // A non-2xx response (503 during a rollout pause, 500, 401) makes the browser
    // give up for good: readyState CLOSED, no auto-retry. Our own backoff must
    // reopen it so live updates come back without a manual page refresh.
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();
    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    const first = expectInstance();

    act(() => first.fireErrorWithReadyState(MockEventSource.CLOSED));
    expect(onConn).toHaveBeenCalledWith({ type: "closed" });

    // No immediate reconnect (backoff), then a NEW EventSource opens at the delay.
    expect(expectInstance()).toBe(first);
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(expectInstance()).not.toBe(first);
  });

  it("CONNECTING error does NOT schedule a manual reopen (browser already auto-retries)", () => {
    // A transient network drop keeps readyState CONNECTING and the browser retries
    // on its own — scheduling our own reopen too would double up into a storm.
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    const first = expectInstance();

    act(() => first.fireErrorWithReadyState(MockEventSource.CONNECTING));
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(expectInstance()).toBe(first);
  });

  it("backoff grows across consecutive failures (2nd retry waits longer than the 1st)", () => {
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    const first = expectInstance();

    // 1st failure → reopens after ~1s.
    act(() => first.fireErrorWithReadyState(MockEventSource.CLOSED));
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    const second = expectInstance();
    expect(second).not.toBe(first);

    // 2nd failure (the retry also failed) → the delay doubled: nothing at +1s, a
    // fresh EventSource only at +2s.
    act(() => second.fireErrorWithReadyState(MockEventSource.CLOSED));
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(expectInstance()).toBe(second);
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(expectInstance()).not.toBe(second);
  });

  it("a successful open resets the backoff (next failure retries at the base delay again)", () => {
    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    const first = expectInstance();

    act(() => first.fireErrorWithReadyState(MockEventSource.CLOSED));
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    const second = expectInstance();
    // The reopened connection succeeds — backoff resets to 0.
    act(() => second.fireOpen());

    // A later failure retries at the base delay again (not the grown one).
    act(() => second.fireErrorWithReadyState(MockEventSource.CLOSED));
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(expectInstance()).not.toBe(second);
  });
});

describe("EventStreamProvider cleanup + thread switch", () => {
  it("unmount → old EventSource closed, remount opens a new connection", () => {
    // renderHook's wrapper can't swap props dynamically; two independent
    // renderHook calls (mount #1, then mount #2) are equivalent:
    // instance #1 closes on unmount. Asserting the second mount picks
    // up a new URL is enough here — the cleanup path is not testable
    // in useEventStream-only tests; integration coverage lives in
    // use-timeline (the old version's rerender + close assert was also
    // a hook behavior; after the Provider split it is no longer typical).
    const { unmount } = renderHook(() => useEventStream(vi.fn(), vi.fn()), {
      wrapper: withProvider(),
    });
    const first = expectInstance();
    expect(first.url).toContain("/api/system");
    expect(first.readyState).toBe(MockEventSource.CONNECTING);

    unmount();
    expect(first.readyState).toBe(MockEventSource.CLOSED);

    renderHook(() => useEventStream(vi.fn(), vi.fn()), { wrapper: withProvider() });
    expect(expectInstance().url).toContain("/api/system");
  });

  it("multiple subscribers share one EventSource (core: no reconnect)", () => {
    // Multiple useEventStream calls in the same Provider → subscribers
    // share one connection. Test: both hooks receive the same message.
    const onSystem1 = vi.fn();
    const onSystem2 = vi.fn();
    const wrapper = withProvider();

    // renderHook only takes one hook at a time; helper runs two simultaneously
    function useTwoSubscribers() {
      useEventStream(onSystem1, vi.fn());
      useEventStream(onSystem2, vi.fn());
    }
    renderHook(() => useTwoSubscribers(), { wrapper });

    // Verify EventSource was constructed only once (lastInstance is global; unchanged means only one new call)
    const inst = expectInstance();
    act(() => {
      inst.fireOpen();
      inst.fireMessage(JSON.stringify({ role: "llm_done", agent_id: 42 }));
    });

    expect(onSystem1).toHaveBeenCalledWith({ role: "llm_done", agent_id: 42 });
    expect(onSystem2).toHaveBeenCalledWith({ role: "llm_done", agent_id: 42 });
  });
});

describe("EventStreamProvider heartbeat frame", () => {
  it("heartbeat frame is liveness-only — NOT fanned out as a business event", () => {
    const onSystem = vi.fn();
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(onSystem, onConn), { wrapper: withProvider() });
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage(JSON.stringify({ role: "heartbeat" }));
    });

    // Parsed cleanly (no parse-failed), but the role==="heartbeat" guard
    // skips the subscriber fan-out — it's not a SystemEvent.
    expect(onSystem).not.toHaveBeenCalled();
    expect(onConn).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: "parse-failed" }),
    );
  });

  it("a real business event after a heartbeat still flows through", () => {
    const onSystem = vi.fn();

    renderHook(() => useEventStream(onSystem, vi.fn()), { wrapper: withProvider() });
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage(JSON.stringify({ role: "heartbeat" }));
      expectInstance().fireMessage(JSON.stringify({ role: "llm_done", agent_id: 7 }));
    });

    expect(onSystem).toHaveBeenCalledTimes(1);
    expect(onSystem).toHaveBeenCalledWith({ role: "llm_done", agent_id: 7 });
  });
});

describe("EventStreamProvider half-dead watchdog", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("45s with NO frame → notifies reconnecting + bumps reconnectNonce", () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    act(() => expectInstance().fireOpen());
    onConn.mockClear();

    const nonceBefore = useStore.getState().reconnectNonce;
    act(() => {
      vi.advanceTimersByTime(45_000);
    });

    expect(onConn).toHaveBeenCalledWith({ type: "reconnecting" });
    expect(useStore.getState().reconnectNonce).toBe(nonceBefore + 1);
  });

  it("any frame (incl. heartbeat) resets the watchdog — no fire before 45s of silence", () => {
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();

    renderHook(() => useEventStream(vi.fn(), onConn), { wrapper: withProvider() });
    act(() => expectInstance().fireOpen());
    onConn.mockClear();
    const nonceBefore = useStore.getState().reconnectNonce;

    // Advance 30s, deliver a heartbeat (resets), advance another 30s.
    // Total elapsed 60s but never 45s of continuous silence → no fire.
    act(() => {
      vi.advanceTimersByTime(30_000);
      expectInstance().fireMessage(JSON.stringify({ role: "heartbeat" }));
      vi.advanceTimersByTime(30_000);
    });

    expect(onConn).not.toHaveBeenCalledWith({ type: "reconnecting" });
    expect(useStore.getState().reconnectNonce).toBe(nonceBefore);
  });
});

// The all-events throttled broadcast shares all the connection machinery
// (useSseConnection) with the global broadcast — tested above. These tests
// cover what differs: the URL is always /api/system/all (not tied to
// activeId), the connection is always open, and agent switches don't
// close/reopen it.
describe("AgentEventStreamProvider all-events broadcast URL", () => {
  function withAgentProvider() {
    return function Wrapper({ children }: { children: ReactNode }) {
      return createElement(AgentEventStreamProvider, { children });
    };
  }

  it("always opens /api/system/all regardless of activeId", () => {
    act(() => useStore.setState({ activeId: 7 }));
    void renderHook(() => useAgentEventStream(vi.fn(), vi.fn()), {
      wrapper: withAgentProvider(),
    });
    expect(expectInstance().url).toContain("/api/system/all");
  });

  it("activeId null → still opens connection (always-on broadcast)", () => {
    act(() => useStore.setState({ activeId: null }));
    void renderHook(() => useAgentEventStream(vi.fn(), vi.fn()), {
      wrapper: withAgentProvider(),
    });
    expect(lastInstance).not.toBeNull();
    expect(expectInstance().url).toContain("/api/system/all");
  });

  it("agent switch → does NOT close the connection (same broadcast serves all agents)", () => {
    act(() => useStore.setState({ activeId: 1 }));
    void renderHook(() => useAgentEventStream(vi.fn(), vi.fn()), {
      wrapper: withAgentProvider(),
    });
    const first = expectInstance();
    expect(first.url).toContain("/api/system/all");

    act(() => useStore.setState({ activeId: 2 }));
    // The old connection should NOT be closed — agent switch doesn't re-key the URL
    expect(first.readyState).not.toBe(MockEventSource.CLOSED);
    // And a new EventSource should NOT be constructed (same URL, same provider)
    // lastInstance should still be the same instance
    expect(expectInstance()).toBe(first);
  });

  it("calling useAgentEventStream outside its Provider → throws (fail-fast)", () => {
    const errSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined /* suppress error-boundary noise */);
    expect(() => renderHook(() => useAgentEventStream(vi.fn(), vi.fn()))).toThrow(
      /useAgentEventStream/,
    );
    errSpy.mockRestore();
  });

  it("batched array frame → fans out each element as individual SystemEvent to subscribers", () => {
    act(() => useStore.setState({ activeId: 42 }));
    const onSystem = vi.fn();
    void renderHook(() => useAgentEventStream(onSystem, vi.fn()), {
      wrapper: withAgentProvider(),
    });
    act(() => {
      expectInstance().fireOpen();
      // Simulate a batched frame from /api/system/all
      expectInstance().fireMessage(
        JSON.stringify([
          { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "hello" },
          { role: "code_delta", agent_id: 7, item_id: "1.0", content: "print" },
          { role: "agent_updated", agent_id: 42, snapshot: { id: 42 } },
        ]),
      );
    });

    expect(onSystem).toHaveBeenCalledTimes(3);
    expect(onSystem).toHaveBeenCalledWith({
      role: "chat_delta",
      agent_id: 7,
      item_id: "1.0",
      content: "hello",
    });
    expect(onSystem).toHaveBeenCalledWith({
      role: "code_delta",
      agent_id: 7,
      item_id: "1.0",
      content: "print",
    });
    expect(onSystem).toHaveBeenCalledWith({
      role: "agent_updated",
      agent_id: 42,
      snapshot: { id: 42 },
    });
  });

  it("a throwing subscriber neither blocks other subscribers nor counts as parse-failed", () => {
    const onSystem = vi.fn();
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();
    const onSystemBoom = vi.fn(() => {
      throw new Error("subscriber bug");
    });
    // Both subscribers on the SAME provider connection (two renderHook calls
    // would create two Providers / two EventSources).
    function useDualSubscriber() {
      useAgentEventStream(onSystem, onConn);
      useAgentEventStream(onSystemBoom, vi.fn());
    }
    void renderHook(() => useDualSubscriber(), {
      wrapper: withAgentProvider(),
    });
    const errSpy = vi.spyOn(console, "error");
    act(() => {
      expectInstance().fireMessage(
        JSON.stringify({ role: "chat_delta", agent_id: 7, item_id: "1.0", content: "hi" }),
      );
    });
    errSpy.mockRestore();

    // The healthy subscriber still got the event…
    expect(onSystem).toHaveBeenCalledTimes(1);
    // …the buggy one threw and was isolated (no parse-failed, no reconnect)…
    expect(onSystemBoom).toHaveBeenCalledTimes(1);
    expect(onConn).not.toHaveBeenCalled();
  });

  it("non-object elements in a batch frame are dropped, not fanned out", () => {
    const onSystem = vi.fn();
    void renderHook(() => useAgentEventStream(onSystem, vi.fn()), {
      wrapper: withAgentProvider(),
    });
    act(() => {
      expectInstance().fireMessage(
        JSON.stringify([
          "corrupt-string-element",
          42,
          { role: "agent_updated", agent_id: 42, snapshot: { id: 42 } },
        ]),
      );
    });
    expect(onSystem).toHaveBeenCalledTimes(1);
    expect(onSystem).toHaveBeenCalledWith({
      role: "agent_updated",
      agent_id: 42,
      snapshot: { id: 42 },
    });
  });

  it("batch with heartbeat inside → heartbeat skipped, rest fanned out", () => {
    const onSystem = vi.fn();
    void renderHook(() => useAgentEventStream(onSystem, vi.fn()), {
      wrapper: withAgentProvider(),
    });
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage(
        JSON.stringify([
          { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "a" },
          { role: "heartbeat" },
          { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "b" },
        ]),
      );
    });

    expect(onSystem).toHaveBeenCalledTimes(2);
    expect(onSystem).toHaveBeenCalledWith({
      role: "chat_delta",
      agent_id: 7,
      item_id: "1.0",
      content: "a",
    });
    expect(onSystem).toHaveBeenCalledWith({
      role: "chat_delta",
      agent_id: 7,
      item_id: "1.0",
      content: "b",
    });
  });

  it("batch subscriber gets the whole frame in ONE call; its per-event callback is not invoked", () => {
    const onSystem = vi.fn();
    const onBatch = vi.fn<(events: SystemEvent[]) => void>();
    void renderHook(
      () => useAgentEventStream(onSystem, vi.fn(), onBatch),
      { wrapper: withAgentProvider() },
    );
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage(
        JSON.stringify([
          { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "a" },
          { role: "code_delta", agent_id: 7, item_id: "2.0", content: "b" },
        ]),
      );
    });

    expect(onBatch).toHaveBeenCalledTimes(1);
    expect(onBatch).toHaveBeenCalledWith([
      { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "a" },
      { role: "code_delta", agent_id: 7, item_id: "2.0", content: "b" },
    ]);
    // The per-event callback is the fallback contract for subscribers
    // without a batch handler — it never fires for a batch subscriber.
    expect(onSystem).not.toHaveBeenCalled();
  });

  it("a throwing batch subscriber never starves per-event subscribers of the same frame", () => {
    const onSystem = vi.fn();
    const onConn = vi.fn<(ev: ConnectionEvent) => void>();
    const onBatchBoom = vi.fn(() => {
      throw new Error("batch subscriber bug");
    });
    function useDualSubscriber() {
      useAgentEventStream(onSystem, onConn);
      useAgentEventStream(vi.fn(), vi.fn(), onBatchBoom);
    }
    void renderHook(() => useDualSubscriber(), {
      wrapper: withAgentProvider(),
    });
    const errSpy = vi.spyOn(console, "error");
    act(() => {
      expectInstance().fireMessage(
        JSON.stringify([
          { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "a" },
          { role: "chat_delta", agent_id: 7, item_id: "1.0", content: "b" },
        ]),
      );
    });
    errSpy.mockRestore();

    // The per-event subscriber received BOTH events of the frame…
    expect(onSystem).toHaveBeenCalledTimes(2);
    // …the batch subscriber threw and was isolated (no parse-failed).
    expect(onBatchBoom).toHaveBeenCalledTimes(1);
    expect(onConn).not.toHaveBeenCalled();
  });

  it("single-event frame → batch subscriber receives a one-element array", () => {
    const onBatch = vi.fn<(events: SystemEvent[]) => void>();
    void renderHook(
      () => useAgentEventStream(vi.fn(), vi.fn(), onBatch),
      { wrapper: withAgentProvider() },
    );
    act(() => {
      expectInstance().fireOpen();
      expectInstance().fireMessage(
        JSON.stringify({ role: "llm_done", agent_id: 7 }),
      );
    });
    expect(onBatch).toHaveBeenCalledTimes(1);
    expect(onBatch).toHaveBeenCalledWith([{ role: "llm_done", agent_id: 7 }]);
  });
});


describe("fold subscription lifecycle (Task #1033 regression)", () => {
  it("fold subscription survives the Provider re-render that SSE onopen triggers", () => {
    const { qc, wrapper } = withProviderAndClient();
    // Seed the agents cache so the fold's empty-cache guard accepts the merge.
    qc.setQueryData(["agents"], []);
    renderHook(() => useEventStream(() => undefined, () => undefined), { wrapper });

    // First open: onOpenChange(true) → setSseOpen(true) → Provider re-render.
    // Before the fix that re-render ran the fold effect's cleanup
    // (unsubscribe) and the foldSubscribedRef early-return skipped the
    // resubscribe — the fold was permanently gone from the subscriber set
    // and every domain's realtime layer went silently stale.
    act(() => expectInstance().fireOpen());

    // A later business event must still reach the fold and land in the cache.
    // (fireMessage mirrors the wire format: JSON string — a non-string payload
    // goes down the parse-failed path and never reaches subscribers.)
    act(() => {
      expectInstance().fireMessage(
        JSON.stringify({
          role: "agent_spawned",
          agent_id: 2,
          snapshot: { ...baseAgent, agent_id: 2, label: "spawned-after-open" },
        }),
      );
    });

    const agents = qc.getQueryData<AgentRow[]>(["agents"]);
    expect(agents?.map((a) => a.agent_id)).toEqual([2]);
  });

  it("useFoldOwner returns a stable reference across re-renders", () => {
    const { result, rerender } = renderHook(() => useFoldOwner(), {
      wrapper: withProvider(),
    });
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });

  it("reconnect reconcile throttles the full invalidate to one per 30s window", () => {
    vi.useFakeTimers();
    try {
      const { qc, wrapper } = withProviderAndClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      renderHook(() => useEventStream(() => undefined, () => undefined), { wrapper });

      // Initial open → the full repair invalidate fires.
      act(() => expectInstance().fireOpen());
      expect(spy).toHaveBeenCalledTimes(1);

      // Flaky-network reconnect burst (another open inside the 30s window —
      // mobile Safari CONNECTING/OPEN jitter, watchdog force-reopen) → no
      // second refetch storm.
      spy.mockClear();
      act(() => { vi.advanceTimersByTime(5_000); });
      act(() => expectInstance().fireOpen());
      expect(spy).not.toHaveBeenCalled();

      // Window elapsed → the next open repairs again.
      act(() => { vi.advanceTimersByTime(26_000); });
      act(() => expectInstance().fireOpen());
      expect(spy).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
