"use client";

// Where plugin nav entries land in the console.
//
// Two renderings of the same declarations, because the surfaces differ: a
// toolbar wants icons with the label in the tooltip, a settings section wants
// labelled rows. Both attribute the entry to its plugin — a surface the console
// did not write says whose it is — and both link to the console route that
// frames the plugin's page.
//
// Neither renders anything when no plugin declares an entry for that location:
// an empty toolbar group or an empty settings block would be chrome for a
// feature the cluster is not using.

import Link from "next/link";

import { pluginNavIcon } from "@/components/plugin-nav-icon";
import { FLEX, FLEX_COL } from "@/lib/layout";
import { pluginPageRoute, usePluginNav, type NavLocation } from "@/lib/plugin-nav";
import { cn } from "@/lib/utils";

/** Icon links for a toolbar row — the sidebar footer and the fleet header. */
export function PluginNavIcons({ location }: { location: NavLocation }) {
  const entries = usePluginNav(location);
  if (entries.length === 0) return null;
  return (
    <div className={cn("items-center gap-0.5", FLEX)}>
      {entries.map((entry) => {
        const Icon = pluginNavIcon(entry.icon);
        return (
          <Link
            key={`${entry.plugin}/${entry.page}`}
            href={pluginPageRoute(entry.plugin, entry.page)}
            aria-label={`${entry.label} (${entry.plugin})`}
            title={`${entry.label} (${entry.plugin})`}
            className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground"
          >
            <Icon className="size-4" />
          </Link>
        );
      })}
    </div>
  );
}

/** Labelled rows for a settings section. */
export function PluginNavList({ location }: { location: NavLocation }) {
  const entries = usePluginNav(location);
  if (entries.length === 0) return null;
  return (
    <div className={cn("gap-2", FLEX, FLEX_COL)}>
      <h3 className="text-sm font-semibold text-muted-foreground">Plugin pages</h3>
      <div className="divide-y divide-border rounded-md border border-border">
        {entries.map((entry) => {
          const Icon = pluginNavIcon(entry.icon);
          return (
            <Link
              key={`${entry.plugin}/${entry.page}`}
              href={pluginPageRoute(entry.plugin, entry.page)}
              className={cn(
                "items-center gap-3 px-3 py-2.5 transition-colors hover:bg-sidebar-accent",
                FLEX,
              )}
            >
              <Icon className="size-4 shrink-0 text-muted-foreground" />
              <span className="text-sm font-medium">{entry.label}</span>
              <span className="text-xs text-muted-foreground">{entry.plugin}</span>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
