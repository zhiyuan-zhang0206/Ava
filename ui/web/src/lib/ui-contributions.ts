"use client";

// The console's one read of `GET /api/ui/contributions` — what the cluster's
// enabled plugins declare under `contributions.ui`, merged and attributed.
//
// One query key for the whole declaration set, consumed by each surface's own
// hook (`use-theme-packs` for skins, `plugin-nav` for nav entries): the surfaces
// are independent but the data is one fetch, and one writer per cache key is
// the frontend state rule. Server data, so it lives in TanStack Query; it
// changes only when a plugin is installed, enabled, or upgraded, which is why
// nothing pushes it.

import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import type { UiContributionsResponse } from "./types";

export const UI_CONTRIBUTIONS_QUERY_KEY = ["ui-contributions"] as const;

export function useUiContributions(): {
  contributions: UiContributionsResponse | undefined;
  isLoading: boolean;
} {
  const { data, isLoading } = useQuery({
    queryKey: UI_CONTRIBUTIONS_QUERY_KEY,
    queryFn: () => api.getUiContributions(),
  });
  return { contributions: data, isLoading };
}
