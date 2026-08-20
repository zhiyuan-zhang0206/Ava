"use client";

// Plugin nav entries — the `nav` half of `contributions.ui`.
//
// A nav entry is a link, not a component: `{location, label, icon, page}`, all
// four from closed vocabularies validated at manifest load. The console places
// it on the surface `location` names, draws `icon` with a lucide component it
// already imports, and points it at the plugin's own page — served by the
// gateway from the plugin package (`/api/plugin-ui/<plugin>/…`) and embedded in
// a sandboxed iframe. No third-party code enters this bundle at any step.

import type { Route } from "next";

import { API_BASE } from "./api";
import type { UiNavContribution } from "./types";
import { useUiContributions } from "./ui-contributions";

/** The console surfaces a nav entry may claim.
 *
 * The closed set `shared/plugin_ui_contributions.py:NAV_LOCATIONS` validates
 * against, mirrored here as a value so the two halves can be asserted equal
 * (`plugin-nav-icon.test.ts`): a location the validator accepts but no surface
 * renders would drop a plugin's entry with nothing to see. */
export const NAV_LOCATIONS = ["sidebar", "settings", "fleet-toolbar"] as const;
export type NavLocation = (typeof NAV_LOCATIONS)[number];

/** The nav entries declared for one console surface, in plugin order. */
export function usePluginNav(location: NavLocation): UiNavContribution[] {
  const { contributions } = useUiContributions();
  return (contributions?.nav ?? []).filter((entry) => entry.location === location);
}

/** The console route that frames a plugin page.
 *
 * Cast to `Route` because typedRoutes can only infer a literal it can see;
 * this path is composed from declaration data, and the route it targets
 * (`app/plugin/[plugin]/[[...path]]`) is a catch-all that accepts it. */
export function pluginPageRoute(plugin: string, page: string): Route {
  return `/plugin/${encodeURIComponent(plugin)}/${encodePagePath(page)}` as Route;
}

/** The gateway URL the iframe loads — the plugin's own mount.
 *
 * Absolute (API_BASE), because the console and the gateway are different
 * origins in the default deployment; that also means the framed document gets
 * the gateway's origin rather than the console's. */
export function pluginPageSrc(plugin: string, page: string): string {
  return `${API_BASE}/api/plugin-ui/${encodeURIComponent(plugin)}/${encodePagePath(page)}`;
}

/** Percent-encode each segment while keeping the separators — including a
 *  trailing slash, which is what makes `page: "board/"` a directory URL whose
 *  relative asset links resolve inside the mount. */
function encodePagePath(page: string): string {
  return page.split("/").map(encodeURIComponent).join("/");
}
