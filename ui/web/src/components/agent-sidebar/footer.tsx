"use client";

import {
  Activity,
  AlertTriangle,
  BarChart3,
  Loader2,
  NotebookText,
  RotateCw,
  Settings,
  Waypoints,
} from "lucide-react";
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
  fetching,
  windowHours,
  onWindowChange,
  onRetry,
}: {
  stats: StatsDashboard | undefined;
  error: unknown;
  fetching: boolean;
  windowHours: StatsWindowHours;
  onWindowChange: (h: StatsWindowHours) => void;
  onRetry: () => void;
}) {
  const t = useTranslations("sidebar");
  const errMsg = error ? formatErrMsg(error) : null;
  const failedWithoutData = errMsg !== null && stats === undefined;
  const firstLoad = stats === undefined && error === null && fetching;
  const placeholder = failedWithoutData ? "!" : "—";
  const win = STATS_WINDOW_LABELS[windowHours];
  const windowMismatch = stats !== undefined && stats.window_hours !== windowHours;
  const windowedPlaceholder = windowMismatch ? "…" : placeholder;
  const windowedTitle = windowMismatch ? t("statisticsUpdatingFor", { win }) : null;
  const cards: (
    | { kind?: undefined; label: string; value: string; title?: string; windowed?: boolean; wide?: boolean }
    | { kind: "warnings"; title?: string; windowed?: boolean }
  )[] = [
    {
      label: t("liveAgents"),
      value: stats ? String(stats.live_count) : placeholder,
      title: errMsg ?? t("liveAgentsTitle"),
    },
    {
      label: t("tokens"),
      windowed: true,
      value: windowMismatch
        ? windowedPlaceholder
        : stats
        ? formatTokensCompact(stats.tokens.input + stats.tokens.output)
        : placeholder,
      title:
        windowedTitle ??
        errMsg ??
        (stats
          ? t("tokensTitleDetail", { inp: stats.tokens.input.toLocaleString(), out: stats.tokens.output.toLocaleString(), cache: stats.tokens.cache_hit_pct })
          : t("tokensTitle", { win })),
    },
    {
      label: t("cacheHit"),
      windowed: true,
      value: windowMismatch
        ? windowedPlaceholder
        : stats
          ? `${stats.tokens.cache_hit_pct.toFixed(2)}%`
          : placeholder,
      title: windowedTitle ?? errMsg ?? t("cacheHitTitle", { win }),
    },
    {
      label: t("cost"),
      windowed: true,
      value: windowMismatch
        ? windowedPlaceholder
        : stats
          ? `$${stats.cost_usd.toFixed(2)}`
          : placeholder,
      title:
        windowedTitle ??
        errMsg ??
        (stats
          ? t("costTitleDetail", { win, amount: stats.cost_usd })
          : t("costTitle", { win })),
    },
    {
      label: t("avgTurnTime"),
      wide: true,
      windowed: true,
      value:
        windowMismatch
          ? windowedPlaceholder
          : stats?.avg_turn_seconds != null
          ? `${Math.round(stats.avg_turn_seconds)}s`
          : placeholder,
      title: windowedTitle ?? errMsg ?? t("avgTurnTitle", { win }),
    },
    {
      kind: "warnings",
      windowed: true,
      title: windowedTitle ?? errMsg ?? t("warningsTitle", { win }),
    },
  ];
  const valueClass = failedWithoutData
    ? "font-mono tabular-nums text-sm text-destructive"
    : "font-mono tabular-nums text-sm";
  const placeholderClass = failedWithoutData
    ? "font-mono tabular-nums text-sm text-destructive"
    : "font-mono tabular-nums text-sm";
  return (
    <div className="text-xs">
      <div className={cn("items-center justify-between border-b border-border px-3 py-2", FLEX)}>
        <div className={cn("items-center gap-1.5", FLEX)}>
          <span className="text-[10px] tracking-wide text-muted-foreground">
            {t("statistics")}
          </span>
          {fetching ? (
            <span
              role="status"
              aria-label={t("statisticsUpdating")}
              title={t("statisticsUpdating")}
            >
              <Loader2 className="size-3 animate-spin text-muted-foreground" aria-hidden />
            </span>
          ) : null}
          {failedWithoutData ? (
            <button
              type="button"
              onClick={onRetry}
              aria-label={t("statisticsRetry")}
              title={errMsg}
              className="rounded p-0.5 text-destructive hover:bg-sidebar-accent"
            >
              <RotateCw className="size-3" aria-hidden />
            </button>
          ) : errMsg !== null ? (
            <span role="img" aria-label={errMsg} title={errMsg}>
              <AlertTriangle className="size-3 text-destructive" aria-hidden />
            </span>
          ) : null}
        </div>
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
        {cards.map((card) =>
          card.kind === "warnings" ? (
            <WarningErrorCard
              key="warnings"
              stats={windowMismatch ? undefined : stats}
              placeholder={windowedPlaceholder}
              placeholderClass={placeholderClass}
              firstLoad={firstLoad}
              title={windowMismatch ? card.title : undefined}
            />
          ) : (
            <div
              key={card.label}
              title={windowMismatch && card.windowed ? card.title : undefined}
              className={cn(
                "gap-0.5 px-2 py-1.5 rounded bg-sidebar-accent/40",
                card.wide && "col-span-2",
                FLEX,
                FLEX_COL,
              )}
            >
              <span className="text-[10px] tracking-wide text-muted-foreground">
                {card.label}
              </span>
              {firstLoad ? (
                <span
                  className="h-4 w-10 animate-pulse rounded bg-muted-foreground/20"
                  aria-hidden
                />
              ) : (
                <span className={valueClass}>{card.value}</span>
              )}
            </div>
          ),
        )}
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
  const { stats, error: statsError, isFetching, refetch } = useStatsDashboard(windowHours);

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
            className="z-50 w-[17.625rem] rounded-md border border-border bg-popover text-popover-foreground shadow-md outline-none"
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

// ── Warning / Error three-way card (task #1935) ──
//
// The user-visible trio per level: total / resolved (dismissed) / remaining
// (net). The backend derives all three from the same per-class counts and
// the active `event_dismissals` rows, so resolved + remaining == total by
// construction. A zero remaining renders the positive all-clear state
// instead of a bare 0. `stats` is passed undefined during a window
// transition so the card shows the same "…" placeholder as the other cards
// instead of displaying a previous window's totals.
function WarningErrorCard({
  stats,
  placeholder,
  placeholderClass,
  firstLoad,
  title,
}: {
  stats: StatsDashboard | undefined;
  placeholder: string;
  placeholderClass: string;
  firstLoad: boolean;
  title: string | undefined;
}) {
  const t = useTranslations("sidebar");
  const rows = stats
    ? [
        {
          level: "warning",
          total: stats.warnings,
          dismissed: stats.warnings_dismissed,
          net: stats.warnings_net,
        },
        {
          level: "error",
          total: stats.errors,
          dismissed: stats.errors_dismissed,
          net: stats.errors_net,
        },
      ]
    : null;
  return (
    <div
      title={title}
      className={cn(
        "gap-0.5 rounded bg-sidebar-accent/40 px-2 py-1.5 col-span-2",
        FLEX,
        FLEX_COL,
      )}
    >
      <span className="text-[10px] tracking-wide text-muted-foreground">
        {t("warningsErrors")}
      </span>
      {firstLoad ? (
        <span
          className="h-4 w-10 animate-pulse rounded bg-muted-foreground/20"
          aria-hidden
        />
      ) : rows === null ? (
        <span className={placeholderClass}>{placeholder}</span>
      ) : (
        rows.map((row) => (
          <div
            key={row.level}
            className={cn("flex-wrap items-center gap-0.5 font-mono text-[11px] tabular-nums", FLEX)}
          >
            <span className="w-7 shrink-0 whitespace-nowrap text-muted-foreground">
              {row.level === "warning" ? t("warningLevel") : t("errorLevel")}
            </span>
            <span className="whitespace-nowrap text-foreground">
              {t("statsTotal")} {row.total.toLocaleString()}
            </span>
            <span className="whitespace-nowrap text-muted-foreground">
              · {t("statsDismissed")} {row.dismissed.toLocaleString()}
            </span>
            <span className="ml-auto shrink-0 whitespace-nowrap">
              {row.net === 0 ? (
                <span className="rounded bg-emerald-500/15 px-1 text-emerald-600 dark:text-emerald-400">
                  {t("statsAllClear")}
                </span>
              ) : (
                <span className="text-foreground">
                  {t("statsNet")} {row.net.toLocaleString()}
                </span>
              )}
            </span>
          </div>
        ))
      )}
    </div>
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
