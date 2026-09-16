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
import type { ConfigFieldView } from "./types";

const DISPLAY_CONFIG_QUERY_KEY = ["config", null] as const;

/** The config view as this hook may receive it — the field list can be absent
 *  in a malformed payload; both an absent list and an absent field fall back. */
interface DisplayConfigView {
  readonly fields?: readonly ConfigFieldView[];
}

/** One `display` domain int field, resolved at runtime from GET /api/config.
 *  Returns `fallback` until the config read lands and whenever the field is
 *  missing, non-numeric, or the read failed. */
export function useDisplayLimit(envVar: string, fallback: number): number {
  const { data } = useQuery({
    queryKey: DISPLAY_CONFIG_QUERY_KEY,
    queryFn: () => api.getConfig(),
  });
  // A malformed payload (an unknown endpoint served as {} — the visual-
  // regression e2e stubs do exactly that) must fall back, never crash: read
  // through a view type whose field list is honestly optional.
  const view: DisplayConfigView | undefined = data;
  const raw = view?.fields?.find((f) => f.env_var === envVar)?.current_value;
  return typeof raw === "number" && Number.isFinite(raw) ? raw : fallback;
}
