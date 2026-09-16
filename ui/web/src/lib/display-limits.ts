// Runtime-resolved display-window defaults (task #3696): the server's
// `display` config domain is the source of truth for user-facing windows;
// each call site's baked value is only a fallback. One React Query entry
// (structurally the same key as the Config panel's default selection,
// ["config", null], so the reads are shared) backs every consumer; a failed
// or missing read keeps the fallback — the value never blanks or clears
// (user ruling 2026-09-17, task #3696).

"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "./api";

const DISPLAY_CONFIG_QUERY_KEY = ["config", null] as const;

/** One `display` domain int field, resolved at runtime from GET /api/config.
 *  Returns `fallback` until the config read lands and whenever the field is
 *  missing, non-numeric, or the read failed. */
export function useDisplayLimit(envVar: string, fallback: number): number {
  const { data } = useQuery({
    queryKey: DISPLAY_CONFIG_QUERY_KEY,
    queryFn: () => api.getConfig(),
  });
  const raw = data?.fields.find((f) => f.env_var === envVar)?.current_value;
  return typeof raw === "number" && Number.isFinite(raw) ? raw : fallback;
}
