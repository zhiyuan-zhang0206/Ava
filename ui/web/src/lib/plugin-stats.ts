"use client";

// The statistics-panel join: plugin-declared cards (`contributions.ui.stats`
// — one cached fetch, changed only by plugin installs) x their runtime values
// (`plugin_stats` rows riding the polled dashboard response). The join is
// pure and lives in `buildPluginStatCards`; the hook is the thin React
// binding the panel uses.
//
// A declared card with no value row renders as an explicit empty state ("—"):
// the plugin exists, but nothing has refreshed the card yet — or this
// machine has no credential for it. A value older than the stale horizon
// renders dimmed (age in the tooltip), so a refresh that stopped running
// cannot pass for a fresh number.

import { useMemo } from "react";

import type { PluginStat, UiStatContribution } from "./types";
import { useUiContributions } from "./ui-contributions";

/** How old a value may get before the panel marks it stale. Generous on
 *  purpose: plugin refreshes are throttled (minutes), and a stale marker that
 *  fires during normal cadence trains the eye to ignore it. */
export const PLUGIN_STAT_STALE_AFTER_MS = 30 * 60 * 1000;

export type PluginStatStatus = PluginStat["status"];

export interface PluginStatCard {
  /** `plugin/id` — the join key, and the React key. */
  key: string;
  label: string;
  /** null = no value row: the declared card's empty state. */
  value: string | null;
  status: PluginStatStatus | null;
  detail: string | null;
  updatedAt: string | null;
  stale: boolean;
}

/** Join declared cards with their runtime values, declaration order kept. */
export function buildPluginStatCards(
  declarations: UiStatContribution[] | undefined,
  values: PluginStat[] | undefined,
  now: Date = new Date(),
): PluginStatCard[] {
  const byKey = new Map<string, PluginStat>();
  for (const value of values ?? []) byKey.set(`${value.plugin}/${value.id}`, value);

  return (declarations ?? []).map((declaration) => {
    const value = byKey.get(`${declaration.plugin}/${declaration.id}`);
    const updatedMs = value !== undefined ? Date.parse(value.updated_at) : Number.NaN;
    return {
      key: `${declaration.plugin}/${declaration.id}`,
      label: declaration.label,
      value: value?.value ?? null,
      status: value?.status ?? null,
      detail: value?.detail ?? null,
      updatedAt: value?.updated_at ?? null,
      stale:
        value !== undefined &&
        Number.isFinite(updatedMs) &&
        now.getTime() - updatedMs > PLUGIN_STAT_STALE_AFTER_MS,
    };
  });
}

/** The panel's view of the plugin cards: declarations joined with the values
 *  the dashboard response carried. */
export function usePluginStatCards(values: PluginStat[] | undefined): PluginStatCard[] {
  const { contributions } = useUiContributions();
  const declarations = contributions?.stats;
  return useMemo(() => buildPluginStatCards(declarations, values), [declarations, values]);
}
