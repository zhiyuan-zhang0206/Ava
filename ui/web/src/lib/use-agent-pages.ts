// useAgentPages — the active agent's currently-open pages (registered HTML
// servers), shown in the inspector's Page section.
//
// ["agent-pages", agentId] reads the server's list. The fold owner in
// EventStreamProvider coalesces page_opened/page_closed hints into invalidation
// and trails a GET already in flight when the event arrived. Reconnect repairs
// events missed while disconnected. This hook only reads the Query result.
//
// The inspector mounts this only while open, so the subscription's lifetime
// is the panel's; a page that opens while the inspector is closed is picked
// up by the cold fetch on the next open (the fold owner lives at the app root).

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
    // Page events invalidate through the fold owner; there is no polling.
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
