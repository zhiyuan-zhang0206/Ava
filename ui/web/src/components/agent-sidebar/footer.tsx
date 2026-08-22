"use client";

import { Activity, BarChart3, NotebookText, Settings, Waypoints } from "lucide-react";
import * as Popover from "@radix-ui/react-popover";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";

import { errMsg as formatErrMsg } from "@/lib/errors";
import { PluginNavIcons } from "@/components/plugin-nav";
import {
  STATS_WINDOW_LABELS,
  STATS_WINDOWS,
  useStatsDashboard,
  useStatsWindow,
  type StatsWindowHours,
} from "@/lib/sidebar";
import type { StatsDashboard } from "@/lib/types";
import { FLEX, FLEX_COL } from "@/lib/layout";
import { cn } from "@/lib/utils";

// ── Stats cards (unchanged 2×3 grid) ──

export function StatsCards({
  stats,
  error,
  windowHours,
  onWindowChange,
}: {
  stats: StatsDashboard | undefined;
  error: unknown;
  windowHours: StatsWindowHours;
  onWindowChange: (h: StatsWindowHours) => void;
}) {
  const t = useTranslations("sidebar");
  const errMsg = error ? formatErrMsg(error) : null;
  const placeholder = errMsg ? "!" : "—";
  const win = STATS_WINDOW_LABELS[windowHours];
  const cards: { label: string; value: string; title?: string }[] = [
    {
      label: t("liveAgents"),
      value: stats ? String(stats.live_count) : placeholder,
      title: errMsg ?? t("liveAgentsTitle"),
    },
    {
      label: t("tokens"),
      value: stats
        ? formatTokensCompact(stats.tokens.input + stats.tokens.output)
        : placeholder,
      title:
        errMsg ??
        (stats
          ? t("tokensTitleDetail", { inp: stats.tokens.input.toLocaleString(), out: stats.tokens.output.toLocaleString(), cache: stats.tokens.cache_hit_pct })
          : t("tokensTitle", { win })),
    },
    {
      label: t("cacheHit"),
      value: stats ? `${stats.tokens.cache_hit_pct.toFixed(2)}%` : placeholder,
      title: errMsg ?? t("cacheHitTitle", { win }),
    },
    {
      label: t("cost"),
      value: stats ? `$${stats.cost_usd.toFixed(2)}` : placeholder,
      title:
        errMsg ??
        (stats
          ? t("costTitleDetail", { win, amount: stats.cost_usd })
          : t("costTitle", { win })),
    },
    {
      label: t("avgTurnTime"),
      value:
        stats?.avg_turn_seconds != null
          ? `${Math.round(stats.avg_turn_seconds)}s`
          : placeholder,
      title: errMsg ?? t("avgTurnTitle", { win }),
    },
    {
      label: t("warningsErrors"),
      value: stats ? `${stats.warnings} / ${stats.errors}` : placeholder,
      title: errMsg ?? t("warningsTitle", { win }),
    },
  ];
  const valueClass = errMsg
    ? "font-mono tabular-nums text-sm text-destructive"
    : "font-mono tabular-nums text-sm";
  return (
    <div className="text-xs">
      <div className={cn("items-center justify-between border-b border-border px-3 py-2", FLEX)}>
        <span className="text-[10px] tracking-wide text-muted-foreground">
          {t("statistics")}
        </span>
        <select
          value={windowHours}
          onChange={(e) => onWindowChange(Number(e.target.value) as StatsWindowHours)}
          aria-label={t("statisticsWindow")}
          className="bg-transparent text-[10px] text-muted-foreground hover:text-foreground rounded px-1 py-0.5 cursor-pointer focus:outline-none"
        >
          {STATS_WINDOWS.map((h) => (
            <option key={h} value={h}>
              {STATS_WINDOW_LABELS[h]}
            </option>
          ))}
        </select>
      </div>
      <div className="grid grid-cols-2 gap-1 px-3 py-2">
        {cards.map((card) => (
          <div
            key={card.label}
            className={cn("gap-0.5 px-2 py-1.5 rounded bg-sidebar-accent/40", FLEX, FLEX_COL)}
          >
            <span className="text-[10px] tracking-wide text-muted-foreground">
              {card.label}
            </span>
            <span className={valueClass}>{card.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Sidebar footer: fixed bottom strip (user ruling 2026-08-05) ──
//
// The spot an app's avatar row would occupy: Statistics (a chart icon that
// opens a small popover panel) on the left, and the four nav shortcuts
// (Memory Graph / Fleet / Insights / Control) on the right. These moved here
// from the header bar; the collapsed rail keeps icon-only versions.

export function SidebarFooter() {
  const t = useTranslations("sidebar");
  const navT = useTranslations("nav");
  const router = useRouter();
  const { windowHours, setWindowHours } = useStatsWindow();
  const { stats, error: statsError } = useStatsDashboard(windowHours);

  return (
    <div className={cn("items-center justify-between border-t border-border px-2 py-1.5", FLEX)}>
      {/* Statistics popover — chart icon opens a small panel with the 2×3
          stats grid + window selector (the old inline stats bar, now
          icon-triggered). */}
      <Popover.Root>
        <Popover.Trigger asChild>
          <button
            type="button"
            aria-label={t("statistics")}
            className="p-1.5 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition-colors"
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
              windowHours={windowHours}
              onWindowChange={setWindowHours}
            />
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>

      <div className={cn("items-center gap-0.5", FLEX)}>
        <SidebarNavButton
          onClick={() => router.push("/memory/graph")}
          label={navT("memoryGraph")}
        >
          <NotebookText className="size-4" />
        </SidebarNavButton>
        <SidebarNavButton onClick={() => router.push("/fleet")} label={navT("fleet")}>
          <Waypoints className="size-4" />
        </SidebarNavButton>
        <SidebarNavButton onClick={() => router.push("/insights")} label={navT("insights")}>
          <Activity className="size-4" />
        </SidebarNavButton>
        <SidebarNavButton onClick={() => router.push("/control")} label={navT("control")}>
          <Settings className="size-4" />
        </SidebarNavButton>
        {/* Plugin-contributed entries come last, after the console's own;
            renders nothing when no plugin declares one for the sidebar. */}
        <PluginNavIcons location="sidebar" />
      </div>
    </div>
  );
}


/** One icon nav shortcut in the sidebar footer. */
function SidebarNavButton({
  onClick,
  label,
  children,
}: {
  onClick: () => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="p-1.5 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground transition-colors"
    >
      {children}
    </button>
  );
}

function formatTokensCompact(n: number): string {
  if (n < 1_000) return String(n);
  if (n < 1_000_000) return `${(n / 1_000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

// Placeholder row for an in-flight spawn. The row itself is direct feedback to
// a user-initiated click, but its *motion* (pulse dot + spinner) counts as a
// dynamic signal and follows the status-color opt-in: quiet mode renders a
