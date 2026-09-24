import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./telemetry", () => ({ track: vi.fn() }));

import { track } from "./telemetry";
import { ProfileSseTransport, reportSseTransportMode, sharedSseSupported, type SseListener } from "./sse-share";

class FakeChannel {
  static channels = new Set<FakeChannel>();
  onmessage: ((event: MessageEvent<unknown>) => void) | null = null;

  constructor() { FakeChannel.channels.add(this); }
  postMessage(data: unknown): void {
    for (const peer of FakeChannel.channels) {
      if (peer !== this) peer.onmessage?.({ data } as MessageEvent<unknown>);
    }
  }
  close(): void { FakeChannel.channels.delete(this); }
}

class FakeDocument extends EventTarget {
  visibilityState: DocumentVisibilityState = "visible";
  show(visible: boolean): void {
    this.visibilityState = visible ? "visible" : "hidden";
    this.dispatchEvent(new Event("visibilitychange"));
  }
}

interface QueuedLock {
  callback: (lock: Lock) => Promise<void>;
  signal: AbortSignal;
  resolve: () => void;
  reject: (error: unknown) => void;
}

class FakeLocks {
  private held = false;
  private queue: QueuedLock[] = [];
  failure: Error | null = null;

  request(
    name: string,
    options: LockOptions,
    callback: (lock: Lock) => Promise<void>,
  ): Promise<void> {
    if (this.failure !== null) return Promise.reject(this.failure);
    expect(name).toBe("ava-ui-sse");
    expect(options.mode).toBe("exclusive");
    const signal = options.signal;
    if (!signal) throw new Error("missing lock abort signal");
    return new Promise<void>((resolve, reject) => {
      const item = { callback, signal, resolve, reject };
      this.queue.push(item);
      signal.addEventListener("abort", () => {
        if (!this.queue.includes(item)) return;
        this.queue = this.queue.filter((queued) => queued !== item);
        reject(new DOMException("aborted", "AbortError"));
      });
      this.pump();
    });
  }

  private pump(): void {
    if (this.held) return;
    const item = this.queue.shift();
    if (!item) return;
    this.held = true;
    void item.callback({ name: "ava-ui-sse", mode: "exclusive" })
      .then(item.resolve, item.reject)
      .finally(() => {
        this.held = false;
        this.pump();
      });
  }
}

class FakeSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static failOnPath: string | null = null;
  readyState = FakeSource.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    if (FakeSource.failOnPath !== null && url.endsWith(FakeSource.failOnPath)) {
      FakeSource.failOnPath = null;
      throw new Error("source construction failed");
    }
  }
  close(): void { this.readyState = FakeSource.CLOSED; }
  open(): void {
    this.readyState = FakeSource.OPEN;
    this.onopen?.();
  }
  frame(data: string): void { this.onmessage?.({ data } as MessageEvent<string>); }
  error(state: number): void {
    this.readyState = state;
    this.onerror?.();
  }
}

function tab(locks: FakeLocks) {
  const document = new FakeDocument();
  const channel = new FakeChannel();
  const sources: FakeSource[] = [];
  const checkAuth = vi.fn(() => Promise.resolve({ authenticated: true }));
  const sessionInvalid = vi.fn();
  const transport = new ProfileSseTransport({
    channel: channel as unknown as BroadcastChannel,
    locks: locks as unknown as LockManager,
    document: document as unknown as Document,
    source: (url: string) => {
      const source = new FakeSource(url);
      sources.push(source);
      return source as unknown as EventSource;
    },
    checkAuth,
    sessionInvalid,
    now: Date.now,
  });
  const listen = (ch: "system" | "alerts" | "systemAll", activeId: number | null = null) => {
    const onFrame = vi.fn<(raw: string) => void>();
    const onState = vi.fn<(state: "open" | "reconnecting" | "closed") => void>();
    const unsubscribe = transport.subscribe(ch, { onFrame, onState } satisfies SseListener, activeId);
    return { onFrame, onState, unsubscribe };
  };
  return { transport, document, channel, sources, checkAuth, sessionInvalid, listen };
}

async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("EventSource", FakeSource);
  FakeChannel.channels.clear();
  FakeSource.failOnPath = null;
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  FakeChannel.channels.clear();
});

describe("profile SSE transport", () => {
  it("only the leader constructs sources and relays open state, heartbeat, and frames", async () => {
    const locks = new FakeLocks();
    const leader = tab(locks);
    const follower = tab(locks);
    leader.listen("system");
    const received = follower.listen("system");
    await settle();

    expect(leader.transport.isLeader()).toBe(true);
    expect(follower.transport.isLeader()).toBe(false);
    expect(leader.sources.map((source) => new URL(source.url).pathname)).toEqual([
      "/api/system", "/api/alerts/stream",
    ]);
    expect(follower.sources).toHaveLength(0);
    leader.sources[0].open();
    leader.sources[0].frame('{"role":"heartbeat"}');
    leader.sources[0].frame('{"role":"agent_started"}');
    expect(received.onState).toHaveBeenCalledWith("open");
    expect(received.onFrame.mock.calls.map(([raw]) => raw)).toEqual([
      '{"role":"heartbeat"}', '{"role":"agent_started"}',
    ]);
    leader.transport.dispose();
    follower.transport.dispose();
    await settle();
  });

  it("promotes a waiting visible tab after leader close", async () => {
    const locks = new FakeLocks();
    const leader = tab(locks);
    const follower = tab(locks);
    leader.listen("system");
    const received = follower.listen("system");
    await settle();
    leader.sources[0].open();

    leader.transport.dispose();
    await settle();
    expect(leader.sources.every((source) => source.readyState === FakeSource.CLOSED)).toBe(true);
    expect(follower.transport.isLeader()).toBe(true);
    expect(follower.sources).toHaveLength(2);
    expect(received.onState).toHaveBeenLastCalledWith("reconnecting");
    follower.sources[0].open();
    expect(received.onState).toHaveBeenLastCalledWith("open");
    follower.transport.dispose();
    await settle();
  });

  it("hide aborts a pending lock, closes a held lock, and show requests again", async () => {
    const locks = new FakeLocks();
    const leader = tab(locks);
    const follower = tab(locks);
    leader.listen("system");
    follower.listen("system");
    await settle();
    follower.document.show(false);
    leader.document.show(false);
    await settle();
    expect(leader.sources.every((source) => source.readyState === FakeSource.CLOSED)).toBe(true);
    expect(follower.sources).toHaveLength(0);

    follower.document.show(true);
    await settle();
    expect(follower.transport.isLeader()).toBe(true);
    expect(follower.sources).toHaveLength(2);
    leader.transport.dispose();
    follower.transport.dispose();
    await settle();
  });

  it("opens a sorted detail union and removes changed or stale interests", async () => {
    const locks = new FakeLocks();
    const leader = tab(locks);
    const follower = tab(locks);
    leader.listen("systemAll", 7);
    const detail = follower.listen("systemAll", 3);
    await settle();
    await vi.advanceTimersByTimeAsync(UNION_DEBOUNCE_MS_FOR_TEST);
    expect(leader.sources.at(-1)?.url).toContain("/api/system/all?agents=3,7");
    leader.sources.at(-1)?.open();
    const firstDetail = leader.sources.at(-1);

    detail.unsubscribe();
    await vi.advanceTimersByTimeAsync(UNION_DEBOUNCE_MS_FOR_TEST);
    expect(firstDetail?.readyState).toBe(FakeSource.CLOSED);
    expect(leader.sources.at(-1)?.url).toContain("/api/system/all?agents=7");
    expect(leader.sources.at(-1)?.readyState).toBe(FakeSource.CONNECTING);

    follower.listen("systemAll", 3);
    await vi.advanceTimersByTimeAsync(UNION_DEBOUNCE_MS_FOR_TEST);
    expect(leader.sources.at(-1)?.url).toContain("/api/system/all?agents=3,7");
    // Simulate a crashed tab: it cannot send leave or answer pings.
    follower.channel.close();
    await vi.advanceTimersByTimeAsync(50_250);
    expect(leader.sources.at(-1)?.url).toContain("/api/system/all?agents=7");
    leader.transport.dispose();
    follower.transport.dispose();
    await settle();
  });

  it("mirrors errors without a follower auth probe or follower source", async () => {
    const locks = new FakeLocks();
    const leader = tab(locks);
    const follower = tab(locks);
    leader.listen("alerts");
    const received = follower.listen("alerts");
    await settle();
    leader.sources[1].error(FakeSource.CONNECTING);
    expect(received.onState).toHaveBeenCalledWith("reconnecting");
    leader.sources[1].error(FakeSource.CLOSED);
    await settle();
    expect(received.onState).toHaveBeenCalledWith("closed");
    expect(leader.checkAuth).toHaveBeenCalledTimes(1);
    expect(follower.checkAuth).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(leader.sources).toHaveLength(3);
    expect(follower.sources).toHaveLength(0);
    leader.transport.dispose();
    follower.transport.dispose();
    await settle();
  });

  it("does not replay a stale open after a restart changes the state", async () => {
    const leader = tab(new FakeLocks());
    leader.listen("system");
    await settle();
    leader.sources[0].open();
    const late = leader.listen("system");
    leader.transport.restart("system");
    leader.transport.restart("system");
    await settle();

    expect(late.onState.mock.calls.map(([state]) => state)).toEqual(["reconnecting"]);
    expect(leader.sources.filter((source) => source.url.endsWith("/api/system"))).toHaveLength(2);
    leader.transport.dispose();
    await settle();
  });

  it("detects missing platform APIs so Providers retain their local fallback", () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    expect(sharedSseSupported()).toBe(false);
    vi.unstubAllGlobals();
    const original = navigator.locks;
    Object.defineProperty(navigator, "locks", { configurable: true, value: undefined });
    expect(sharedSseSupported()).toBe(false);
    Object.defineProperty(navigator, "locks", { configurable: true, value: original });
  });

  it("handles a non-abort Web Lock rejection without an unhandled promise", async () => {
    const locks = new FakeLocks();
    locks.failure = new Error("lock manager failed");
    const failed = tab(locks);
    const report = vi.spyOn(console, "error").mockImplementation(() => undefined);
    failed.listen("system");
    await settle();
    expect(failed.transport.isLeader()).toBe(false);
    expect(failed.sources).toHaveLength(0);
    expect(report).toHaveBeenCalledWith("[sse-share] Web Lock failed", locks.failure);
    failed.transport.dispose();
    report.mockRestore();
  });

  it("clears leadership and closes partial sources when the lock callback throws", async () => {
    const failed = tab(new FakeLocks());
    FakeSource.failOnPath = "/api/alerts/stream";
    const report = vi.spyOn(console, "error").mockImplementation(() => undefined);
    failed.listen("system");
    await settle();
    expect(failed.transport.isLeader()).toBe(false);
    expect(failed.sources).toHaveLength(1);
    expect(failed.sources[0].readyState).toBe(FakeSource.CLOSED);
    expect(report).toHaveBeenCalledWith("[sse-share] Web Lock failed", expect.any(Error));
    failed.transport.dispose();
    report.mockRestore();
  });

  it("reports the fallback mode and capability flags once per tab session", () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    const original = navigator.locks;
    Object.defineProperty(navigator, "locks", { configurable: true, value: undefined });
    reportSseTransportMode();
    reportSseTransportMode();
    expect(track).toHaveBeenCalledTimes(1);
    expect(track).toHaveBeenCalledWith("sse-transport", {
      key: "fallback", value: expect.stringContaining("locks=0,bc=0") as string,
    });
    Object.defineProperty(navigator, "locks", { configurable: true, value: original });
  });
});

const UNION_DEBOUNCE_MS_FOR_TEST = 250;
