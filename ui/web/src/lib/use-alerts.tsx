"use client";

// Alerts live data — the Alert system's SSE provider + cache hooks
// (Task #1224). Alert is fully separate from Notice: own table, own UI, own
// IM channel.
//
// One EventSource to /api/alerts/stream feeds the TanStack Query ["alerts"]
// prefix: every frame (one AlertRow JSON per ingest) folds into every
// matching cache (the badge query with default params, the section query
// with includeRead=true) — no polling for SSE-backed data (frontend AGENTS.md
// state rule). The initial GET /api/alerts is the fetch fallback for rows
// ingested before the subscription opened.
//
// Machinery mirrors the useEventStream provider (watchdog + closed-backoff
// reopen + heartbeat skip), scoped down to the one alert shape: frames that
// fail to parse are dropped, a wedged socket reopens, a CLOSED stream
// reconnects with capped backoff.
//
// Cache shape: AlertsResponse (alerts + meta.unresolved_count). Frames upsert
// by row id and apply unresolved-count deltas.

import {
  useCallback,
  useEffect,
  useState,
  type ReactNode,
} from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { API_BASE, api } from "./api";
import type { Alert, AlertsResponse } from "./types";

// Half-dead-connection watchdog window (same value as useEventStream: the
// server emits a heartbeat data frame after ~15s of silence).
const WATCHDOG_MS = 45_000;
// SSE-connect-failure retry: EventSource auto-retries transient drops, but a
// non-2xx response leaves it CLOSED permanently — schedule our own reopen
// with capped exponential backoff.
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

/** The default-params cache key (badge / provider warm-up). */
export const ALERTS_QUERY_KEY = ["alerts"] as const;

/** The section's cache key (includeRead=true — the history list). */
export const ALERTS_SECTION_QUERY_KEY = ["alerts", "section"] as const;

/** A heartbeat frame from the stream — liveness only, never business data. */
function isHeartbeat(raw: unknown): boolean {
  return (
    typeof raw === "object" &&
    raw !== null &&
    !Array.isArray(raw) &&
    (raw as { role?: unknown }).role === "heartbeat"
  );
}

function isAlertRow(raw: unknown): raw is Alert {
  return (
    typeof raw === "object" &&
    raw !== null &&
    !Array.isArray(raw) &&
    typeof (raw as { id?: unknown }).id === "number" &&
    typeof (raw as { status?: unknown }).status === "string"
  );
}

/** Fold one SSE frame into an existing cache: upsert by id and apply the
 *  unresolved-count delta so the badge stays exact. */
export function foldAlert(prev: AlertsResponse | undefined, row: Alert): AlertsResponse {
  if (!prev) {
    return {
      alerts: [row],
      meta: {
        window: "24h",
        include_read: true,
        total: 1,
        unresolved_count: row.status === "unresolved" ? 1 : 0,
      },
    };
  }
  const existing = prev.alerts.find((a) => a.id === row.id);
  const alerts = existing
    ? prev.alerts.map((a) => (a.id === row.id ? row : a))
    : [row, ...prev.alerts].slice(0, 200);
  let unresolved = prev.meta.unresolved_count;
  if (existing) {
    if (existing.status === "resolved" && row.status === "unresolved") unresolved += 1;
    if (existing.status === "unresolved" && row.status === "resolved") unresolved -= 1;
  } else {
    if (row.status === "unresolved") unresolved += 1;
  }
  return {
    alerts,
    meta: { ...prev.meta, unresolved_count: Math.max(0, unresolved) },
  };
}

export function AlertsProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  // The badge count stays warm for the whole app via this always-on
  // default-params query (the initial fetch fallback).
  useQuery<AlertsResponse>({
    queryKey: ALERTS_QUERY_KEY,
    queryFn: () => api.getAlerts({}),
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });

  const [reconnectNonce, setReconnectNonce] = useState(0);
  const [retryNonce, setRetryNonce] = useState(0);

  // Stable reconnect callback (mirrors useEventStream's bumpReconnect).
  const bumpReconnect = useCallback(() => setReconnectNonce((n) => n + 1), []);

  useEffect(() => {
    let failCount = 0;
    let parseFailures = 0;
    let watchdog: ReturnType<typeof setTimeout> | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    const armWatchdog = () => {
      if (watchdog !== null) clearTimeout(watchdog);
      watchdog = setTimeout(() => {
        bumpReconnect();
      }, WATCHDOG_MS);
    };

    const scheduleReopen = () => {
      if (retryTimer !== null) return;
      const delay = Math.min(RECONNECT_BASE_MS * 2 ** failCount, RECONNECT_MAX_MS);
      failCount += 1;
      retryTimer = setTimeout(() => {
        retryTimer = null;
        setRetryNonce((n) => n + 1);
      }, delay);
    };

    const foldFrame = (raw: string) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw);
      } catch {
        parseFailures += 1;
        if (parseFailures >= 3) {
          parseFailures = 0;
          scheduleReopen();
        }
        return;
      }
      if (isHeartbeat(parsed) || !isAlertRow(parsed)) return;
      // Fold into every ["alerts", ...] cache (the badge query and the
      // section query — prefix match keeps one writer per cache).
      const caches = queryClient.getQueryCache().findAll({ queryKey: ALERTS_QUERY_KEY });
      for (const cache of caches) {
        queryClient.setQueryData<AlertsResponse>(cache.queryKey, (old) => foldAlert(old, parsed));
      }
    };

    const es = new EventSource(`${API_BASE}/api/alerts/stream`, { withCredentials: true });
    es.onopen = () => {
      failCount = 0;
      armWatchdog();
    };
    es.onmessage = (e) => {
      armWatchdog();
      const raw = typeof e.data === "string" ? e.data : "";
      if (!raw) return;
      foldFrame(raw);
    };
    es.onerror = () => {
      switch (es.readyState) {
        case EventSource.CLOSED:
          scheduleReopen();
          return;
        case EventSource.CONNECTING:
          // The browser is already auto-retrying — don't double up.
          return;
        case EventSource.OPEN:
          return; // transient fault absorbed by the browser
        default:
          throw new Error(`unknown EventSource readyState: ${es.readyState}`);
      }
    };

    return () => {
      if (watchdog !== null) clearTimeout(watchdog);
      if (retryTimer !== null) clearTimeout(retryTimer);
      es.close();
    };
    // reconnectNonce + retryNonce are the reopen levers; the cache folders are
    // stable identities included only to satisfy the lint.
  }, [queryClient, reconnectNonce, retryNonce, bumpReconnect]);

  return children;
}

/** The badge count + rows (default params, warmed by the provider). */
export function useAlerts() {
  return useQuery<AlertsResponse>({ queryKey: ALERTS_QUERY_KEY, staleTime: Infinity });
}

/** The alert section's history (includeRead=true — the full list). */
export function useAlertsSection() {
  return useQuery({
    queryKey: ALERTS_SECTION_QUERY_KEY,
    queryFn: () => api.getAlerts({ window: "24h", includeRead: true, limit: 200 }),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
}
