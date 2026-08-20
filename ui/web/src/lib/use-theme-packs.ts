"use client";

// Plugin-contributed skins — the read side of `contributions.ui.themes`.
//
// A theme pack is a partial map of the console's own `:root` color tokens
// (globals.css) to color literals, validated at manifest load
// (`shared/plugin_ui_contributions.py`). The console never runs plugin code to
// render one: it sets the declared custom properties on the root element and
// the app's existing components re-read them like any other token change.
//
// Server data, so it lives in TanStack Query like every other server read. The
// set changes only when a plugin is installed, enabled, or upgraded, which is
// why nothing pushes it — the provider-level staleTime is plenty.

import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import type { UiThemeContribution } from "./types";

export const UI_CONTRIBUTIONS_QUERY_KEY = ["ui-contributions"] as const;

/** The id `display.theme_pack` stores, and the value the picker round-trips.
 *
 * Plugin-qualified because a theme name is only unique inside its plugin: two
 * plugins may each ship a "solarized", and the user's stored choice has to
 * survive that without silently switching to the other one's palette. */
export function themePackId(theme: UiThemeContribution): string {
  return `${theme.plugin}/${theme.name}`;
}

/** Every theme pack the cluster's enabled plugins contribute. */
export function useThemePacks(): {
  packs: UiThemeContribution[];
  isLoading: boolean;
} {
  const { data, isLoading } = useQuery({
    queryKey: UI_CONTRIBUTIONS_QUERY_KEY,
    queryFn: () => api.getUiContributions(),
  });
  return { packs: data?.themes ?? [], isLoading };
}
