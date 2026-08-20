"use client";

// Plugin-contributed skins — the themes half of `contributions.ui`.
//
// A theme pack is a partial map of the console's own `:root` color tokens
// (globals.css) to color literals, validated at manifest load
// (`shared/plugin_ui_contributions.py`). The console never runs plugin code to
// render one: it sets the declared custom properties on the root element and
// the app's existing components re-read them like any other token change.

import { useUiContributions } from "./ui-contributions";
import type { UiThemeContribution } from "./types";

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
  const { contributions, isLoading } = useUiContributions();
  return { packs: contributions?.themes ?? [], isLoading };
}
