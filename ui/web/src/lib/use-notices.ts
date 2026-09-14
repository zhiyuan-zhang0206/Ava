// useNotices — the Inbox panel's single data contract (Task #1024, R4
// layer 2, decision Q1=A). One panel = one request = one hook: GET
// /api/notices carries the whole queue — the OPEN split by kind (open =
// FYI, awaiting = require_response) plus one keyset page of the RESOLVED
// history with its next_cursor. This replaces the three-pipe client merge
// (agent snapshot + useInboxFeed + useResolvedNotices): no more hand-rolled
// reconciliation, and "queue empty" is never indistinguishable from "couldn't
// load" (stale-while-error on both halves).
//
// Liveness: the gateway broadcasts notice_posted for every new notice and
// notice_resolved for every resolution — the R4 fold owner (lib/fold/notices)
// invalidates the open query on both (a resolve can move a row out of
// awaiting or open) and the resolved history on notice_resolved, so a
// just-resolved notice drops off the open queue and reappears at the top of
// the history in the same beat. Reconnect repair is the fold owner's central
// invalidate-all. This hook only reads.

"use client";

import { useInfiniteQuery, useQuery, type QueryClient } from "@tanstack/react-query";

import { api } from "./api";
import type { NoticeItem } from "./types";

/** The open-queue query (open + awaiting in one response). */
export const NOTICES_QUERY_KEY = ["notices"] as const;
/** The resolved-history infinite query. */
export const NOTICES_RESOLVED_QUERY_KEY = ["notices-resolved"] as const;

const OPEN_LIMIT = 200;
const PAGE_SIZE = 30;

interface ResolvedCursor {
  beforeAt: string;
  beforeId: number;
}


/** Wire shape of GET /api/notices as cached under NOTICES_QUERY_KEY (the
 *  useQuery data — camelCase fields, unlike the hook's flattened NoticesFeed). */
export interface NoticesFeedWire {
  open: NoticeItem[];
  awaiting: NoticeItem[];
  resolved_page: NoticeItem[];
  next_cursor: { before_at: string; before_id: number } | null;
}

export interface NoticesFeed {
  /** Open FYI notices (require_response=false), priority then newest. */
  open: NoticeItem[];
  /** Open require_response notices — the "waiting on you" worklist. */
  awaiting: NoticeItem[];
  /** Resolved history (both kinds), newest resolution first. */
  resolved: NoticeItem[];
  fetchNextPage: () => void;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  /** The open-queue fetch failed. Stale-while-error: `open`/`awaiting` keep
   *  their last data; the view flags the failure so "queue empty" is never
   *  indistinguishable from "couldn't load". */
  error: boolean;
  /** The resolved-history fetch failed (same stale-while-error semantics). */
  resolvedError: boolean;
  /** First load in flight with nothing loaded yet. */
  isLoading: boolean;
}

/** Ids dropped optimistically whose server snapshot may still list them: one
 *  refetch initiated after the drop is enough to revive a row until the
 *  server resolve lands (task #3272). The deadline bounds how long a drop
 *  can hide a row when no confirmation ever arrives. */
const dropTombstones = new Map<number, number>(); // id -> deadline (ms epoch)
const TOMBSTONE_MS = 15_000;

/** Drop tombstoned ids from one incoming open-queue snapshot. A snapshot
 *  that no longer lists an id proves the server resolved it — retire that
 *  tombstone; the deadline retires any tombstone no confirmation reaches. */
function withoutDroppedRows(wire: NoticesFeedWire): NoticesFeedWire {
  if (dropTombstones.size === 0) return wire;
  const now = Date.now();
  for (const [id, deadline] of dropTombstones) {
    if (deadline <= now) dropTombstones.delete(id);
  }
  const listed = new Set([...wire.open.map((n) => n.id), ...wire.awaiting.map((n) => n.id)]);
  for (const id of [...dropTombstones.keys()]) {
    if (!listed.has(id)) dropTombstones.delete(id);
  }
  if (dropTombstones.size === 0) return wire;
  return {
    ...wire,
    open: wire.open.filter((n) => !dropTombstones.has(n.id)),
    awaiting: wire.awaiting.filter((n) => !dropTombstones.has(n.id)),
  };
}

export function useNotices(): NoticesFeed {
  const feedQuery = useQuery({
    queryKey: NOTICES_QUERY_KEY,
    queryFn: async () => withoutDroppedRows(await api.getNotices({ limit: OPEN_LIMIT })),
  });

  const resolvedQuery = useInfiniteQuery({
    queryKey: NOTICES_RESOLVED_QUERY_KEY,
    queryFn: ({ pageParam }) =>
      api.getNotices({
        limit: OPEN_LIMIT,
        resolvedLimit: PAGE_SIZE,
        ...pageParam,
      }),
    initialPageParam: undefined as ResolvedCursor | undefined,
    // The endpoint returns next_cursor in wire shape (snake_case); the api
    // layer expects camelCase params, so translate here. None = end.
    getNextPageParam: (lastPage): ResolvedCursor | undefined =>
      lastPage.next_cursor == null
        ? undefined
        : { beforeAt: lastPage.next_cursor.before_at, beforeId: lastPage.next_cursor.before_id },
  });

  return {
    open: feedQuery.data?.open ?? [],
    awaiting: feedQuery.data?.awaiting ?? [],
    resolved: resolvedQuery.data?.pages.flatMap((p) => p.resolved_page) ?? [],
    fetchNextPage: () => void resolvedQuery.fetchNextPage(),
    hasNextPage: resolvedQuery.hasNextPage,
    isFetchingNextPage: resolvedQuery.isFetchingNextPage,
    error: feedQuery.isError,
    resolvedError: resolvedQuery.isError,
    isLoading: feedQuery.isLoading || resolvedQuery.isLoading,
  };
}


/** Remove notices from the open-queue cache — the instant local half of a
 *  resolve (Task #1814). The Inbox used to wait for the SSE notice_resolved
 *  round trip + the fold's debounced refetch, so a mark-read row stayed
 *  visible ~2s after the click; dropping it here makes the queue reflect the
 *  read immediately. The SSE-triggered refetch reconciles a beat later (and
 *  is a no-op against server truth), and the resolved history still arrives
 *  from the server. Only the open feed is touched — awaiting entries are
 *  filtered too (defensive).
 *
 *  Any in-flight open-queue refetch is cancelled first so a pre-resolve
 *  snapshot cannot land over the drop and resurrect the row (task #3269),
 *  and the dropped ids stay tombstoned for a beat so a refetch newly
 *  initiated after the drop cannot either (task #3272). */
export function dropOpenNotices(queryClient: QueryClient, noticeIds: number[]): void {
  if (noticeIds.length === 0) return;
  // Arm the tombstone before the optimistic drop: a refetch newly initiated
  // after this point (e.g. an SSE invalidation inside the resolve window)
  // still returns the pre-resolve snapshot, and would revive the rows for
  // the seconds until the resolve lands without this guard (task #3272).
  const deadline = Date.now() + TOMBSTONE_MS;
  for (const id of noticeIds) dropTombstones.set(id, deadline);
  // Cancel the in-flight open-queue refetch first: its snapshot predates this
  // drop, and letting it land would resurrect the just-resolved row until the
  // next SSE-driven refetch corrects it — visible as the row bouncing back
  // during a resolve burst (task #3269). Same guard as the settings optimistic
  // update (use-user-settings.ts).
  void queryClient.cancelQueries({ queryKey: NOTICES_QUERY_KEY });
  const gone = new Set(noticeIds);
  queryClient.setQueryData<NoticesFeedWire>(NOTICES_QUERY_KEY, (old) => {
    if (!old) return old;
    return {
      ...old,
      open: old.open.filter((n) => !gone.has(n.id)),
      awaiting: old.awaiting.filter((n) => !gone.has(n.id)),
    };
  });
}

/** Test-only: clear pending drop tombstones between cases. */
export function __dropTombstonesResetForTest(): void {
  dropTombstones.clear();
}
