// useAgentPages — the active agent's currently-open pages (registered HTML
// servers), shown in the inspector's Page section.
//
// Server data on TanStack Query, kept live by the R4 fold (layer 1):
//   ["agent-pages", agentId] query → api.listPages(agentId) — one initial fetch.
//     staleTime Infinity: after the seed, the fold owns the cache. No
//     refetchInterval, so a poll snapshot can never overwrite a fresher SSE
//     merge (the SSE-vs-load race the old hand-rolled page trackers had).
//   page_opened / page_closed are folded into this cache by the fold owner in
//     EventStreamProvider (lib/fold/pages.ts) — this hook only reads.
//   reconnect → the fold owner invalidates all queries, repairing the
//     disconnect window's missed events.
//
// The inspector mounts this only while open, so the subscription's lifetime
// is the panel's; a page that opens while the inspector is closed is picked
// up by the cold fetch on the next open (the fold keeps the cache fresh
// regardless — the fold owner lives at the app root).

"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { api } from "./api";
import { errMsg } from "./errors";
import type { PageRow } from "./types";

export function useAgentPages(agentId: number): PageRow[] {
  const { data: pages = [], error } = useQuery({
    queryKey: ["agent-pages", agentId] as const,
    queryFn: () => api.listPages(agentId),
    // SSE-driven: page_opened/page_closed fold into this cache, so it stays
    // fresh from one fetch — no polling, no overwrite-on-refetch race.
    staleTime: Infinity,
    gcTime: 30 * 60_000,
  });

  // A failed page list must not blank the inspector — just log (no toast); the
  // section renders its "no open page" empty state.
  useEffect(() => {
    if (error) console.warn(`[agent-pages] listPages failed: ${errMsg(error)}`);
  }, [error]);

  return pages;
}
