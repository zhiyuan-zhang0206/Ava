"use client";

import { Activity, BarChart3, ChevronRight, NotebookText, Search, Settings, Waypoints } from "lucide-react";
import * as Popover from "@radix-ui/react-popover";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";

import { BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS, FLEX, FLEX_COL } from "@/lib/layout";
import { useStatsDashboard, useStatsWindow } from "@/lib/sidebar";
import { cn } from "@/lib/utils";

import { StatsCards } from "./footer";
import { fleetHref } from "./links";
import type { DesktopProps } from "./types";

export function CollapsedSidebar(props: DesktopProps) {
  const t = useTranslations("sidebar");
  const navT = useTranslations("nav");
  const router = useRouter();
  const { setCollapsed, onSearchOpen } = props;

  return (
    <aside className={cn("h-full w-full bg-sidebar", FLEX, FLEX_COL)}>
      {/* Collapsed rail top (user ruling 2026-08-05 21:50): expand sits at
          the very top, search directly below it — stacked vertically, never
          side by side. No divider lines between them. */}
      {/* The top two buttons share the bottom icon column's visual style
          (p-1 rounded muted-foreground → accent hover) — same language as
          the Statistics / nav icons (user ruling 2026-08-06). */}
      <div className={cn("items-center justify-center gap-1", BAR_HEIGHT_CLASS, FLEX, FLEX_COL)}>
        <button
          onClick={() => setCollapsed(false)}
          className="p-1 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition-colors"
          aria-label={t("expandSidebar")}
        >
          <ChevronRight className="size-4" />
        </button>
        <button
          onClick={onSearchOpen}
          className="p-1 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition-colors"
          aria-label={t("searchAgents")}
        >
          <Search className="size-4" />
        </button>
      </div>
      {/* Icon column at the bottom (user ruling 2026-08-05): the nav
          shortcuts stay reachable while collapsed — Statistics popover
          (also icon-triggered here) + the four nav icons, stacked. */}
      <div className={cn("mt-auto items-center gap-1 border-t border-border py-2", FLEX, FLEX_COL)}>
        <CollapsedStatsButton />
        <button
          type="button"
          onClick={() => router.push("/memory/graph")}
          aria-label={navT("memoryGraph")}
          title={navT("memoryGraph")}
          className="p-1 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition-colors"
        >
          <NotebookText className="size-4" />
        </button>
        <button
          type="button"
          onClick={() => router.push(fleetHref(props.activeId))}
          aria-label={navT("fleet")}
          title={navT("fleet")}
          className="p-1 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition-colors"
        >
          <Waypoints className="size-4" />
        </button>
        <button
          type="button"
          onClick={() => router.push("/insights")}
          aria-label={navT("insights")}
          title={navT("insights")}
          className="p-1 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition-colors"
        >
          <Activity className="size-4" />
        </button>
        <button
          type="button"
          onClick={() => router.push("/control")}
          aria-label={navT("control")}
          title={navT("control")}
          className="p-1 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition-colors"
        >
          <Settings className="size-4" />
        </button>
      </div>
    </aside>
  );
}

/** Statistics popover trigger for the collapsed rail (vertical layout). */

function CollapsedStatsButton() {
  const t = useTranslations("sidebar");
  const { windowHours, setWindowHours } = useStatsWindow();
  const { stats, error: statsError, isFetching, refetch } = useStatsDashboard(windowHours);
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button
          type="button"
          aria-label={t("statistics")}
          title={t("statistics")}
          className="p-1 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition-colors"
        >
          <BarChart3 className="size-4" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          sideOffset={6}
          align="start"
          className="z-50 w-64 rounded-md border border-border bg-popover text-popover-foreground shadow-md outline-none"
        >
          <StatsCards
            stats={stats}
            error={statsError}
            fetching={isFetching}
            windowHours={windowHours}
            onWindowChange={setWindowHours}
            onRetry={() => { void refetch(); }}
          />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

// ── Floating search overlay ──
//
// Task #723: search moved from an inline sidebar box to a floating overlay —
// a dimmed backdrop + a centered, shadowed panel (command-palette style), so
// search stays reachable even when the sidebar is collapsed. Task #750 (user
// ruling): the overlay is fully independent — the sidebar list is NOT filtered
// while typing; the query lives in the store only so the overlay input keeps
// its text across open/close. Results are agent-id DESCENDING. Picking a

export function SidebarHeader({
  trailing,
}: {
  trailing: React.ReactNode;
}) {
  return (
    <header
      className={cn(
        "items-center justify-between px-3 py-2",
        BAR_DIVIDER_CLASS,
        BAR_HEIGHT_CLASS,
        FLEX
      )}
    >
      <h1 className="text-base font-semibold tracking-wide">Ava</h1>
      <div className={cn("items-center gap-1.5", FLEX)}>{trailing}</div>
    </header>
  );
}
