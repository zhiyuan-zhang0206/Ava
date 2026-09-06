// Sidebar state hooks: collapsed / view mode / sort / stats window are
// DB-backed user settings (display.sidebar_*), so the choice survives refreshes
// and stays in sync across every frontend entrypoint via the shared
// ["user-settings"] cache. Homepage panel widths are device-local layout ratios
// owned by react-resizable-panels, not durable user settings.
//
// Agent labels don't live here — the backend `threads.label` column is
// the source of truth; the gateway BackgroundTask runs an LLM in the
// background to generate a short label when spawn carries a prompt;
// user overrides go through `api.patchAgentLabel`.

import { keepPreviousData, type QueryClient, type UseQueryResult, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect } from "react";

import { api } from "./api";
import type { StatsDashboard } from "./types";
import { useUserSettings } from "./use-user-settings";

// Collapse remains DB-backed (display.sidebar_collapsed) and syncs across
// frontends within a session, but resets to expanded on every entry (#723).
// The reset waits for settings to load in agent-sidebar/index.tsx.
export function useSidebarCollapsed(): {
  collapsed: boolean;
  setCollapsed: (c: boolean) => void;
} {
  const { settings, setSetting } = useUserSettings();
  const collapsed = settings["display.sidebar_collapsed"] === true;
  const setCollapsed = useCallback(
    (c: boolean) => setSetting("display.sidebar_collapsed", c),
    [setSetting],
  );
  return { collapsed, setCollapsed };
}

// Whether terminated agents are shown lives in the DB-backed user setting
// `display.show_terminated` (read via useUserSettings), not here — that keeps
// the sidebar toggle and the Display settings page pointed at one source of
// truth instead of a localStorage copy that could drift.

// Whether agents are shown as a flat sortable list (default) or a spawn-lineage
// tree. Flat + sort-by-id is the default so the list has a stable, predictable
// order; the tree remains available via the toggle. DB-backed
// (display.sidebar_view_mode).
export function useSidebarViewMode(): {
  viewMode: "tree" | "flat";
  setViewMode: (v: "tree" | "flat") => void;
} {
  const { settings, setSetting } = useUserSettings();
  const viewMode: "tree" | "flat" =
    settings["display.sidebar_view_mode"] === "tree" ? "tree" : "flat";
  const setViewMode = useCallback(
    (v: "tree" | "flat") => setSetting("display.sidebar_view_mode", v),
    [setSetting],
  );
  return { viewMode, setViewMode };
}

// Flat-list sort: which key + direction. DB-backed (display.sidebar_sort).
// Default is id descending (a stable, predictable order); switching to
// last_active defaults to most-recent-first.
export type FlatSortKey = "id" | "last_active" | "status";
export type SortDir = "asc" | "desc";
export interface SidebarSort {
  key: FlatSortKey;
  dir: SortDir;
}
export const SIDEBAR_SORT_DEFAULT: SidebarSort = { key: "id", dir: "desc" };
// Direction a key snaps to when first selected (id/status ascending, last_active
// most-recent-first); clicking the already-active key reverses it.
export const SORT_DEFAULT_DIR: Record<FlatSortKey, SortDir> = {
  id: "desc",
  last_active: "desc",
  status: "asc",
};

function isFlatSortKey(v: unknown): v is FlatSortKey {
  return v === "id" || v === "last_active" || v === "status";
}

export function isSidebarSort(v: unknown): v is SidebarSort {
  if (typeof v !== "object" || v === null) return false;
  const s = v as Record<string, unknown>;
  return isFlatSortKey(s.key) && (s.dir === "asc" || s.dir === "desc");
}

export function useSidebarSort(): {
  sort: SidebarSort;
  setSort: (s: SidebarSort) => void;
} {
  const { settings, setSetting } = useUserSettings();
  const raw = settings["display.sidebar_sort"];
  const sort = isSidebarSort(raw) ? raw : SIDEBAR_SORT_DEFAULT;
  const setSort = useCallback(
    (s: SidebarSort) => setSetting("display.sidebar_sort", s),
    [setSetting],
  );
  return { sort, setSort };
}

// Aggregation windows the stats endpoint accepts (`?hours=`), with the
// compact labels the selector renders. Must mirror the backend whitelist
// (`gateway/schemas/stats.py:StatsWindowHours`) — `0` means the last 5m;
// any other value outside this whitelist 422s.
export const STATS_WINDOWS = [0, 1, 6, 24, 72, 168] as const;
export type StatsWindowHours = (typeof STATS_WINDOWS)[number];
export const STATS_WINDOW_LABELS: Record<StatsWindowHours, string> = {
  0: "5m",
  1: "1h",
  6: "6h",
  24: "24h",
  72: "3d",
  168: "7d",
};
export const STATS_WINDOW_DEFAULT: StatsWindowHours = 24;

// Selected stats window, DB-backed (display.stats_window_hours) so it survives
// refreshes and syncs across frontends. A stored value outside STATS_WINDOWS
// (stale from an older build) is ignored and the default kept.
export function useStatsWindow(): {
  windowHours: StatsWindowHours;
  setWindowHours: (h: StatsWindowHours) => void;
} {
  const { settings, setSetting } = useUserSettings();
  const raw = settings["display.stats_window_hours"];
  const windowHours: StatsWindowHours =
    typeof raw === "number" && (STATS_WINDOWS as readonly number[]).includes(raw)
      ? (raw as StatsWindowHours)
      : STATS_WINDOW_DEFAULT;
  const setWindowHours = useCallback(
    (h: StatsWindowHours) => setSetting("display.stats_window_hours", h),
    [setSetting],
  );
  return { windowHours, setWindowHours };
}

// `/api/stats/dashboard` polling — data source for the sidebar stat cards.
// 30s, not the 5s the cheap polls use: each call runs 4 windowed Loki
// aggregations on the gateway (request COUNT is not request COST), and stat
// cards do not need 5s freshness — keepPreviousData already prevents
// flicker. `error` is exposed to callers so "DB down / endpoint 500" is
// visually distinguishable from "first load hasn't completed" (StatsCards
// renders them differently). `windowHours` is part of the query key, so
// switching the window refetches immediately instead of waiting out the
// poll interval.
const STATS_POLL_MS = 30_000;

interface StatsPollEntry {
  subscribers: number;
  timer: ReturnType<typeof setInterval>;
}

// One coordinator per browser QueryClient (therefore per page). React Query
// deduplicates requests that overlap exactly, but separate observers each own
// their refetchInterval and can drift out of phase after responsive remounts.
// The coordinator gives all consumers of one window a single 30s clock while
// the query cache remains the shared server-truth store.
const statsPollers = new WeakMap<QueryClient, Map<StatsWindowHours, StatsPollEntry>>();

function subscribeStatsPoll(queryClient: QueryClient, windowHours: StatsWindowHours): () => void {
  let byWindow = statsPollers.get(queryClient);
  if (byWindow === undefined) {
    byWindow = new Map();
    statsPollers.set(queryClient, byWindow);
  }
  let entry = byWindow.get(windowHours);
  if (entry === undefined) {
    const timer = setInterval(() => {
      void queryClient
        .fetchQuery({
          queryKey: ["stats", "dashboard", windowHours],
          queryFn: () => api.getStatsDashboard(windowHours),
          retry: 1,
          staleTime: 0,
        })
        // React Query records the error for every observer to render; consume
        // the timer-owned Promise so a failed poll is not an unhandled rejection.
        .catch(() => undefined);
    }, STATS_POLL_MS);
    entry = { subscribers: 0, timer };
    byWindow.set(windowHours, entry);
  }
  entry.subscribers += 1;
  return () => {
    const current = byWindow.get(windowHours);
    if (current === undefined) return;
    current.subscribers -= 1;
    if (current.subscribers === 0) {
      clearInterval(current.timer);
      byWindow.delete(windowHours);
    }
  };
}

export function useStatsDashboard(windowHours: StatsWindowHours): {
  stats: StatsDashboard | undefined;
  error: unknown;
  isFetching: boolean;
  refetch: UseQueryResult<StatsDashboard>["refetch"];
} {
  const queryClient = useQueryClient();
  const { data: stats, error, isFetching, refetch } = useQuery({
    queryKey: ["stats", "dashboard", windowHours],
    queryFn: ({ signal }) => api.getStatsDashboard(windowHours, signal),
    retry: 1,
    // A staggered second observer must consume the page's current snapshot,
    // not start a second request/cadence of its own.
    staleTime: STATS_POLL_MS,
    // Keep previous data visible when switching the window (e.g. 24h → 7d)
    // so the stat cards never flicker to "—" placeholders.
    placeholderData: keepPreviousData,
  });
  useEffect(
    () => subscribeStatsPoll(queryClient, windowHours),
    [queryClient, windowHours],
  );
  return { stats, error, isFetching, refetch };
}

// Relative time formatting for sidebar rows (and, transitively, every other
// consumer of this re-export — alerts, inbox-queue). The exact date is in
// the hover tooltip (ISO string passed as title). Delegates to the shared
// helper (`@/lib/time`) so this and every other relative-time surface in the
// app read the same wording scheme (tz audit, 2026-08: this used to be one
// of 5 independently-drifted implementations).
export { formatRelative as formatRelativeTime } from "./time";
