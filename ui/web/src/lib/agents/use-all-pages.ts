// useAllPages — every agent's currently-open pages (registered HTML servers),
// fleet-wide. The many-agent twin of useAgentPages: GET /api/pages returns
// authoritative rows, and page_opened/page_closed invalidate this Query through
// the fold owner. This hook only reads.
//
// The fold owner coalesces hints and trails an in-flight GET; reconnect repairs
// events missed while disconnected.

"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { api } from "../transport/api";
import { errMsg } from "../contracts/errors";
import type { PageRow } from "../contracts/types";

const ALL_PAGES_QUERY_KEY = ["all-pages"] as const;

export function useAllPages(): PageRow[] {
  const { data: pages = [], error } = useQuery({
    queryKey: ALL_PAGES_QUERY_KEY,
    queryFn: () => api.listAllPages(),
    // Page events invalidate through the fold owner; there is no polling.
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
