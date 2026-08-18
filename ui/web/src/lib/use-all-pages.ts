// useAllPages — every agent's currently-open pages (registered HTML servers),
// fleet-wide. The many-agent twin of useAgentPages: one initial fetch of
// GET /api/pages kept live by the same SSE page_opened / page_closed deltas,
// folded by the R4 fold owner (lib/fold/pages.ts) — this hook only reads.
//
//   ["all-pages"] query → api.listAllPages() — one initial fetch. staleTime
//     Infinity: after the seed, the fold owns the cache (same no-poll-
//     overwrite contract as useAgentPages).
//   reconnect → the fold owner invalidates all queries, repairing missed
//     events.

"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { api } from "./api";
import { errMsg } from "./errors";
import type { PageRow } from "./types";

const ALL_PAGES_QUERY_KEY = ["all-pages"] as const;

export function useAllPages(): PageRow[] {
  const { data: pages = [], error } = useQuery({
    queryKey: ALL_PAGES_QUERY_KEY,
    queryFn: () => api.listAllPages(),
    // SSE-driven: page_opened/page_closed fold into this cache, so it stays
    // fresh from one fetch — no polling, no overwrite-on-refetch race.
    staleTime: Infinity,
    gcTime: 30 * 60_000,
  });

  // A failed page list must not break the inbox — just log; the page affordances
  // simply do not render.
  useEffect(() => {
    if (error) console.warn(`[all-pages] listAllPages failed: ${errMsg(error)}`);
  }, [error]);

  return pages;
}
