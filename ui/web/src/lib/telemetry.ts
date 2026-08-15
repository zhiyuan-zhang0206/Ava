// Frontend user-modeling telemetry — the single tracking entry point.
//
// Pipeline: track() → in-memory buffer → batched POST to the gateway's
// /api/frontend-telemetry (sendBeacon when the tab is hiding, fetch +
// keepalive otherwise) → `frontend_interaction` rows in the events table →
// the Grafana core panels (interactions / top elements / page views /
// settings changes).
//
// Volume discipline (task #1092 — the events table must not be flooded):
//   1. dedupe — the same (page, element, key) at most once per 2 s;
//   2. rate cap — at most 100 events per minute per tab, excess dropped;
//   3. buffer cap — 200 pending events, oldest dropped.
// The gateway backstops with its own per-session limit, so a bug here can
// only waste this tab's budget, never the table.
//
// No free text is ever collected: `element` is a closed union of known
// interaction points, `page` is the normalized router pathname, and `value`
// is a sanitized ≤64-char scalar (bool / number / short string) used only
// for settings-change events. Session id is a random per-tab uuid — it
// groups one browser session without carrying identity.

import { API_BASE } from "./api";

const TELEMETRY_ENDPOINT = "/api/frontend-telemetry";
// Dedupe window per (page, element, key) — a double-click or a slider
// chattering is one interaction, not N.
const DEDUPE_MS = 2000;
// Per-tab rate cap: honest single-user usage is tens of events/min.
const RATE_LIMIT = 100;
const RATE_WINDOW_MS = 60_000;
// Pending buffer cap — drop oldest past this (the tab is offline for a
// while; the oldest interactions are worth the least).
const BUFFER_CAP = 200;
// Flush cadence while the tab is visible.
const FLUSH_INTERVAL_MS = 15_000;

/** Closed vocabulary of tracked interaction points — adding one is a
 * deliberate instrumentation decision, never a free string. */
export type TelemetryElement =
  | "page-view"
  | "composer-send"
  | "composer-stop"
  | "spawn"
  | "fork"
  | "terminate"
  | "restart"
  | "resurrect"
  | "compact"
  | "setting-change";

export interface TrackOptions {
  /** Override the current page (defaults to the last TelemetryPageView
   *  route). Normalized form, e.g. "fleet" / "control/config". */
  page?: string;
  /** settings key — setting-change events only. */
  key?: string;
  /** settings value — sanitized to a ≤64-char scalar string. */
  value?: unknown;
}

interface PendingEvent {
  page: string;
  element: string;
  key?: string;
  value?: string;
  ts: number;
}

// ── module state (browser only; everything is a no-op under SSR) ──

let enabled = typeof window !== "undefined";
let sessionId = "";
let currentPage = "unknown";
let buffer: PendingEvent[] = [];
// dedupe: `${page}|${element}|${key}` -> last tracked wall-clock ms
const lastTracked = new Map<string, number>();
// rate window: wall-clock ms of every event that left this tab
const sentAt: number[] = [];
let flushTimer: ReturnType<typeof setInterval> | null = null;
let warnedOnce = false;

function newSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  // Non-secure-context fallback (plain-HTTP tailnet): randomUUID is
  // secure-context-only; uniqueness is all that matters here.
  const rnd = () => Math.random().toString(16).slice(2);
  return `${Date.now().toString(16)}-${rnd()}${rnd()}`;
}

function sanitizeValue(value: unknown): string | undefined {
  if (typeof value === "boolean" || typeof value === "number") {
    return String(value);
  }
  if (typeof value === "string") {
    return value.length > 64 ? value.slice(0, 64) : value;
  }
  // Objects / arrays / undefined — never serialized (a setting value is
  // scalar; anything else is not this module's business).
  return undefined;
}

function rateLimited(): boolean {
  const now = Date.now();
  while (sentAt.length > 0 && now - sentAt[0] > RATE_WINDOW_MS) sentAt.shift();
  // Count pending too: an offline tab must not accumulate a backlog that
  // floods the table the moment the network returns.
  return sentAt.length + buffer.length >= RATE_LIMIT;
}

function flush(): void {
  if (!enabled || buffer.length === 0) return;
  if (sessionId === "") sessionId = newSessionId();
  const events = buffer;
  buffer = [];
  const body = JSON.stringify({ session_id: sessionId, events });
  const url = `${API_BASE}${TELEMETRY_ENDPOINT}`;
  // Beacon first: it survives pagehide/unload and needs no response. When
  // unavailable (or its size limit rejects the batch), fall back to fetch
  // with keepalive.
  let ok = false;
  try {
    if ("sendBeacon" in navigator) {
      ok = navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
    }
  } catch {
    ok = false;
  }
  if (!ok) {
    try {
      void fetch(url, {
        method: "POST",
        credentials: "include",
        keepalive: true,
        headers: { "content-type": "application/json" },
        body,
      }).catch(() => {
        // Best-effort by contract: a lost batch is a lost batch. Warn once
        // so a persistent outage is visible in the console without spamming.
        if (!warnedOnce) {
          warnedOnce = true;
          console.warn("[telemetry] flush failed — interactions dropped");
        }
      });
    } catch {
      // keepalive unsupported / body too large — drop silently
    }
  }
  const now = Date.now();
  events.forEach(() => {
    sentAt.push(now);
  });
}

function ensureTimer(): void {
  if (flushTimer !== null) return;
  flushTimer = setInterval(flush, FLUSH_INTERVAL_MS);
  window.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });
  window.addEventListener("pagehide", flush);
  window.addEventListener("beforeunload", flush);
}

/** Track one interaction. No-op under SSR and while disabled; dedupes and
 *  rate-limits in place. Never throws. */
export function track(element: TelemetryElement, opts: TrackOptions = {}): void {
  if (!enabled) return;
  const page = opts.page ?? currentPage;
  const key = opts.key;
  const dedupeKey = `${page}|${element}|${key ?? ""}`;
  const now = Date.now();
  const last = lastTracked.get(dedupeKey);
  if (last !== undefined && now - last < DEDUPE_MS) return;
  lastTracked.set(dedupeKey, now);
  if (rateLimited()) return;
  const value = sanitizeValue(opts.value);
  const ev: PendingEvent = { page, element, ts: now };
  if (key !== undefined) ev.key = key;
  if (value !== undefined) ev.value = value;
  if (buffer.length >= BUFFER_CAP) buffer.shift();
  buffer.push(ev);
  ensureTimer();
}

/** Set the current page (normalized route) — called by TelemetryPageView
 *  on every route change; subsequent track() calls inherit it. */
export function setTelemetryPage(page: string): void {
  currentPage = page;
}

/** Normalize a router pathname to the telemetry page vocabulary:
 *  "/" → "home", "/shell/42" → "shell" (agent ids collapsed), nested
 *  routes keep their segments ("control/config", "insights/metrics"). */
export function normalizePage(pathname: string): string {
  const p = pathname.replace(/^\/+/, "").replace(/\/+$/, "");
  if (p === "") return "home";
  const [head, second] = p.split("/");
  // `/shell/<id>` collapses to "shell"; a bare `/shell` has no second
  // segment (undefined coerces to "undefined", never a digit — safe).
  if (head === "shell" && /^\d+$/.test(second)) {
    return "shell";
  }
  return p;
}

// ── test surface (not part of the public contract) ──

/** Reset module state — tests only. */
export function __telemetryResetForTest(): void {
  enabled = typeof window !== "undefined";
  sessionId = "";
  currentPage = "unknown";
  buffer = [];
  lastTracked.clear();
  sentAt.length = 0;
  if (flushTimer !== null) {
    clearInterval(flushTimer);
    flushTimer = null;
  }
  warnedOnce = false;
}

/** Force a flush now — tests only. Returns the number of events sent. */
export function __telemetryFlushForTest(): number {
  const n = buffer.length;
  flush();
  return n;
}

/** Pending buffer size — tests only. */
export function __telemetryBufferSize(): number {
  return buffer.length;
}

/** Per-tab session id (minted lazily) — tests only. */
export function __telemetrySessionId(): string {
  if (sessionId === "") sessionId = newSessionId();
  return sessionId;
}

/** Force-disable (SSR-safety probe) — tests only. */
export function __telemetrySetEnabledForTest(value: boolean): void {
  enabled = value;
}
