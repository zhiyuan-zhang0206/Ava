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

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

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

export function useNotices(): NoticesFeed {
  const feedQuery = useQuery({
    queryKey: NOTICES_QUERY_KEY,
    queryFn: () => api.getNotices({ limit: OPEN_LIMIT }),
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
