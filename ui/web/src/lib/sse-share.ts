"use client";

// One visible document holds the profile lock and owns all three gateway SSE
// sockets. Other visible documents receive raw frames and connection state over
// BroadcastChannel. The fallback lives in the existing Providers.

import { API_BASE, api } from "./api";
import { notifySessionInvalid } from "./auth-context";

export type SseChannel = "system" | "alerts" | "systemAll";
export type SseState = "open" | "reconnecting" | "closed";
export interface SseListener {
  onFrame: (raw: string) => void;
  onState: (state: SseState) => void;
}

type Message =
  | { type: "frame"; ch: SseChannel; data: string; leaderId: string }
  | { type: "state"; ch: SseChannel; state: SseState; leaderId: string }
  | { type: "interest"; tabId: string; activeId: number | null }
  | { type: "leave"; tabId: string }
  | { type: "ping"; leaderId: string }
  | { type: "restart"; ch: SseChannel; backoff: boolean }
  | { type: "sessionInvalid"; leaderId: string };

const NAME = "ava-ui-sse";
// A ping requests a fresh interest announcement every 15s. A closed tab is
// removed after 45-50s (three missed announcements plus a 5s sweep);
// explicit leave is faster.
const PING_MS = 15_000;
const INTEREST_TTL_MS = 45_000;
const PRUNE_MS = 5_000;
const UNION_DEBOUNCE_MS = 250;
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

interface Environment {
  channel: BroadcastChannel;
  locks: LockManager;
  document: Document;
  source: (url: string) => EventSource;
  checkAuth: typeof api.checkAuth;
  sessionInvalid: () => void;
  now: () => number;
}

interface SourceSlot {
  source: EventSource | null;
  url: string | null;
  retry: ReturnType<typeof setTimeout> | null;
  failures: number;
  lastRestartAt: number;
}

function newSlot(): SourceSlot {
  return { source: null, url: null, retry: null, failures: 0, lastRestartAt: -Infinity };
}

export function sharedSseSupported(): boolean {
  if (typeof BroadcastChannel === "undefined" || typeof navigator === "undefined") return false;
  const locks: unknown = Reflect.get(navigator, "locks");
  return typeof locks === "object" && locks !== null &&
    "request" in locks && typeof locks.request === "function";
}

export class ProfileSseTransport {
  // Only needs to distinguish concurrent documents; it is not an auth token.
  private readonly tabId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  private readonly listeners: Record<SseChannel, Set<SseListener>> = {
    system: new Set(), alerts: new Set(), systemAll: new Set(),
  };
  private readonly detailInterests = new Map<SseListener, number | null>();
  private readonly states: Record<SseChannel, SseState | null> = {
    system: null, alerts: null, systemAll: null,
  };
  private readonly slots: Record<SseChannel, SourceSlot> = {
    system: newSlot(), alerts: newSlot(), systemAll: newSlot(),
  };
  private readonly interests = new Map<string, { activeId: number | null; seen: number }>();
  private participating = false;
  private leader = false;
  private currentLeader: string | null = null;
  private pendingLock: AbortController | null = null;
  private releaseLock: (() => void) | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private pruneTimer: ReturnType<typeof setInterval> | null = null;
  private unionTimer: ReturnType<typeof setTimeout> | null = null;
  private detailUnion = "";
  private disposed = false;

  constructor(private readonly env: Environment) {
    env.channel.onmessage = (event: MessageEvent<unknown>) => this.receive(event.data);
    env.document.addEventListener("visibilitychange", this.syncParticipation);
  }

  subscribe(ch: SseChannel, listener: SseListener, activeId: number | null = null): () => void {
    this.listeners[ch].add(listener);
    if (ch === "systemAll") this.detailInterests.set(listener, activeId);
    if (this.participating) this.announceInterest();
    this.syncParticipation();
    const state = this.states[ch];
    if (state !== null && this.participating) {
      queueMicrotask(() => {
        if (this.listeners[ch].has(listener) && this.participating && this.states[ch] === state)
          listener.onState(state);
      });
    }
    return () => {
      this.listeners[ch].delete(listener);
      if (ch === "systemAll") this.detailInterests.delete(listener);
      if (this.participating) this.announceInterest();
      this.syncParticipation();
    };
  }

  isLeader(): boolean { return this.leader; }

  /** A follower can request repair; only the lock holder touches EventSource. */
  restart(ch: SseChannel, backoff = false): void {
    if (!this.participating) return;
    if (this.leader) this.restartOwned(ch, backoff);
    else this.post({ type: "restart", ch, backoff });
  }

  /** Also useful for document teardown; a browser releases the lock on close. */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.env.document.removeEventListener("visibilitychange", this.syncParticipation);
    this.stopParticipation();
    queueMicrotask(() => this.env.channel.close());
  }

  private readonly syncParticipation = (): void => {
    const hasListeners = Object.values(this.listeners).some((set) => set.size > 0);
    const shouldParticipate = !this.disposed && hasListeners &&
      this.env.document.visibilityState === "visible";
    if (shouldParticipate === this.participating) return;
    if (!shouldParticipate) {
      this.stopParticipation();
      return;
    }
    this.participating = true;
    this.announceInterest();
    this.requestLock();
  };

  private stopParticipation(): void {
    if (!this.participating) return;
    this.participating = false;
    this.post({ type: "leave", tabId: this.tabId });
    this.pendingLock?.abort();
    this.releaseLock?.();
    this.currentLeader = null;
  }

  private requestLock(): void {
    if (this.pendingLock || !this.participating) return;
    const controller = new AbortController();
    this.pendingLock = controller;
    void this.env.locks.request(NAME, { mode: "exclusive", signal: controller.signal }, async () => {
      if (!this.participating) return;
      this.leader = true;
      this.currentLeader = this.tabId;
      this.startLeader();
      await new Promise<void>((resolve) => { this.releaseLock = resolve; });
      this.releaseLock = null;
      this.stopLeader();
      this.leader = false;
    }).catch((error: unknown) => {
      if (error instanceof DOMException && error.name === "AbortError") return;
      this.stopParticipation();
      throw error;
    }).finally(() => {
      this.pendingLock = null;
      if (this.participating) this.requestLock();
    });
  }

  private post(message: Message): void { this.env.channel.postMessage(message); }

  private localActiveId(): number | null {
    for (const id of this.detailInterests.values()) if (id !== null) return id;
    return null;
  }

  private announceInterest(): void {
    const activeId = this.localActiveId();
    if (this.leader) {
      this.interests.set(this.tabId, { activeId, seen: this.env.now() });
      this.scheduleUnion();
    } else {
      this.post({ type: "interest", tabId: this.tabId, activeId });
    }
  }

  private receive(raw: unknown): void {
    if (!raw || typeof raw !== "object" || !("type" in raw)) return;
    const message = raw as Message;
    switch (message.type) {
      case "interest": {
        if (!this.leader) return;
        const firstAnnouncement = !this.interests.has(message.tabId);
        this.interests.set(message.tabId, { activeId: message.activeId, seen: this.env.now() });
        this.scheduleUnion();
        // A newly visible follower may have missed the original open message.
        if (firstAnnouncement) {
          this.post({ type: "ping", leaderId: this.tabId });
          for (const ch of ["system", "alerts", "systemAll"] as const) {
            const state = this.states[ch];
            if (state !== null) this.post({ type: "state", ch, state, leaderId: this.tabId });
          }
        }
        return;
      }
      case "leave":
        if (!this.leader) return;
        this.interests.delete(message.tabId);
        this.scheduleUnion();
        return;
      case "ping":
        if (!this.participating || this.leader) return;
        this.currentLeader = message.leaderId;
        this.announceInterest();
        return;
      case "frame":
        if (this.participating && !this.leader && message.leaderId === this.currentLeader)
          this.deliverFrame(message.ch, message.data);
        return;
      case "state":
        if (this.participating && !this.leader && message.leaderId === this.currentLeader)
          this.deliverState(message.ch, message.state);
        return;
      case "restart":
        if (this.leader) this.restartOwned(message.ch, message.backoff);
        return;
      case "sessionInvalid":
        if (this.participating && !this.leader && message.leaderId === this.currentLeader)
          this.env.sessionInvalid();
        return;
      default:
        throw new Error("unknown SSE share message");
    }
  }

  private startLeader(): void {
    this.interests.clear();
    this.announceInterest();
    this.post({ type: "ping", leaderId: this.tabId });
    // A promoted leader may still hold the previous leader's cached open state.
    this.publishState("systemAll", this.localActiveId() === null ? "closed" : "reconnecting");
    this.pingTimer = setInterval(() => {
      this.post({ type: "ping", leaderId: this.tabId });
      this.announceInterest();
    }, PING_MS);
    this.pruneTimer = setInterval(() => {
      const cutoff = this.env.now() - INTEREST_TTL_MS;
      for (const [tabId, interest] of this.interests) {
        if (tabId !== this.tabId && interest.seen <= cutoff) this.interests.delete(tabId);
      }
      this.scheduleUnion();
    }, PRUNE_MS);
    this.openSource("system", `${API_BASE}/api/system`);
    this.openSource("alerts", `${API_BASE}/api/alerts/stream`);
  }

  private stopLeader(): void {
    if (this.pingTimer !== null) clearInterval(this.pingTimer);
    if (this.pruneTimer !== null) clearInterval(this.pruneTimer);
    if (this.unionTimer !== null) clearTimeout(this.unionTimer);
    this.pingTimer = null;
    this.pruneTimer = null;
    this.unionTimer = null;
    for (const ch of ["system", "alerts", "systemAll"] as const) {
      this.closeSource(ch);
      this.publishState(ch, "closed");
    }
    this.detailUnion = "";
    this.interests.clear();
  }

  private scheduleUnion(): void {
    if (!this.leader) return;
    if (this.unionTimer !== null) clearTimeout(this.unionTimer);
    this.unionTimer = setTimeout(() => {
      this.unionTimer = null;
      const ids = [...new Set([...this.interests.values()]
        .map(({ activeId }) => activeId).filter((id): id is number => id !== null))]
        .sort((a, b) => a - b);
      const union = ids.join(",");
      if (union === this.detailUnion) return;
      this.detailUnion = union;
      if (union) this.openSource("systemAll", `${API_BASE}/api/system/all?agents=${union}`);
      else {
        this.closeSource("systemAll");
        this.publishState("systemAll", "closed");
      }
    }, UNION_DEBOUNCE_MS);
  }

  private closeSource(ch: SseChannel): void {
    const slot = this.slots[ch];
    if (slot.retry !== null) clearTimeout(slot.retry);
    slot.retry = null;
    slot.source?.close();
    slot.source = null;
    slot.url = null;
  }

  private openSource(ch: SseChannel, url: string): void {
    this.closeSource(ch);
    this.publishState(ch, "reconnecting");
    const slot = this.slots[ch];
    slot.url = url;
    const source = this.env.source(url);
    slot.source = source;
    source.onopen = () => {
      if (!this.leader || slot.source !== source) return;
      slot.failures = 0;
      this.publishState(ch, "open");
    };
    source.onmessage = (event) => {
      if (!this.leader || slot.source !== source) return;
      this.publishFrame(ch, typeof event.data === "string" ? event.data : "[non-string SSE payload]");
    };
    source.onerror = () => {
      if (!this.leader || slot.source !== source) return;
      switch (source.readyState) {
        case EventSource.CLOSED:
          this.publishState(ch, "closed");
          void this.env.checkAuth().then((result) => {
            if (!this.leader || slot.source !== source) return;
            if (!result.authenticated) {
              this.post({ type: "sessionInvalid", leaderId: this.tabId });
              this.env.sessionInvalid();
            } else this.scheduleRetry(ch);
          }).catch(() => {
            if (this.leader && slot.source === source) this.scheduleRetry(ch);
          });
          return;
        case EventSource.CONNECTING:
          this.publishState(ch, "reconnecting");
          return;
        case EventSource.OPEN:
          return;
        default:
          throw new Error(`unknown EventSource readyState: ${source.readyState}`);
      }
    };
  }

  private scheduleRetry(ch: SseChannel): void {
    const slot = this.slots[ch];
    if (slot.retry !== null || slot.url === null) return;
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** slot.failures, RECONNECT_MAX_MS);
    slot.failures += 1;
    slot.retry = setTimeout(() => {
      slot.retry = null;
      if (this.leader && slot.url !== null) this.openSource(ch, slot.url);
    }, delay);
  }

  private restartOwned(ch: SseChannel, backoff: boolean): void {
    const slot = this.slots[ch];
    if (slot.url === null || slot.retry !== null) return;
    // A watchdog or cluster-update bump can arrive from every visible tab.
    if (this.env.now() - slot.lastRestartAt < RECONNECT_BASE_MS) return;
    slot.lastRestartAt = this.env.now();
    if (backoff) {
      this.publishState(ch, "reconnecting");
      slot.source?.close();
      this.scheduleRetry(ch);
    } else this.openSource(ch, slot.url);
  }

  private deliverFrame(ch: SseChannel, data: string): void {
    for (const listener of this.listeners[ch]) listener.onFrame(data);
  }

  private publishFrame(ch: SseChannel, data: string): void {
    this.deliverFrame(ch, data);
    this.post({ type: "frame", ch, data, leaderId: this.tabId });
  }

  private deliverState(ch: SseChannel, state: SseState): void {
    this.states[ch] = state;
    for (const listener of this.listeners[ch]) listener.onState(state);
  }

  private publishState(ch: SseChannel, state: SseState): void {
    this.deliverState(ch, state);
    this.post({ type: "state", ch, state, leaderId: this.tabId });
  }
}

let shared: ProfileSseTransport | null = null;

export function sharedSseTransport(): ProfileSseTransport {
  if (!sharedSseSupported()) throw new Error("shared SSE APIs unavailable");
  shared ??= new ProfileSseTransport({
    channel: new BroadcastChannel(NAME),
    locks: navigator.locks,
    document,
    source: (url) => new EventSource(url, { withCredentials: true }),
    checkAuth: () => api.checkAuth(),
    sessionInvalid: notifySessionInvalid,
    now: Date.now,
  });
  return shared;
}
