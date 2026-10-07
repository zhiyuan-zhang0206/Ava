"use client";

import {
  Activity,
  AlertTriangle,
  BarChart3,
  ChevronDown,
  ChevronRight,
  Loader2,
  NotebookText,
  RotateCw,
  Settings,
  Waypoints,
} from "lucide-react";
import * as Popover from "@radix-ui/react-popover";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { useRouter } from "next/navigation";

import { PluginNavIcons } from "@/components/plugins/plugin-nav";
import { WindowSelect } from "@/components/agents/window-select";
import { errMsg as formatErrMsg } from "@/lib/contracts/errors";
import { formatTokensCompact } from "@/lib/format/format-number";
import {
  formatRelativeTime,
  STATS_WINDOW_LABELS,
  STATS_WINDOWS,
  useStatsDashboard,
  useStatsWindow,
  type StatsWindowHours,
} from "@/lib/agents/sidebar";
import {
  usePluginStatCards,
  type PluginStatCard as PluginStatCardModel,
} from "@/lib/plugins/plugin-stats";
import type { StatsDashboard } from "@/lib/contracts/types";
import { FLEX, FLEX_COL, MIN_W_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

import { AlertClassList } from "./alert-classes";
import { fleetHref } from "./links";

/** Shared by the expanded footer and collapsed rail. */
export const STATS_POPOVER_CLASS =
  "z-50 max-h-[var(--radix-popover-content-available-height)] w-80 max-w-[calc(100vw-1.5rem)] overflow-y-auto rounded-md border border-border bg-popover text-popover-foreground shadow-md outline-none";

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
  const [alertsOpen, setAlertsOpen] = useState(false);
  const pluginCards = usePluginStatCards(stats?.plugin_stats);
  const errMsg = error ? formatErrMsg(error) : null;
  const failedWithoutData = errMsg !== null && stats === undefined;
  const firstLoad = stats === undefined && error === null && fetching;
  const placeholder = failedWithoutData ? "!" : "—";
  const win = STATS_WINDOW_LABELS[windowHours];
  const windowMismatch = stats !== undefined && stats.window_hours !== windowHours;
  const appliedWindowHours =
    !windowMismatch &&
    stats?.applied_window_hours != null &&
    stats.applied_window_hours < windowHours
      ? stats.applied_window_hours
      : null;
  const windowedPlaceholder = windowMismatch ? "…" : placeholder;
  const windowedTitle = windowMismatch ? t("statisticsUpdatingFor", { win }) : null;
  const cards: (
    | { kind?: undefined; label: string; value: string; title?: string; windowed?: boolean }
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
        <WindowSelect
          value={String(windowHours)}
          options={STATS_WINDOWS.map((h) => ({
            value: String(h),
            label:
              h === windowHours && appliedWindowHours != null
                ? `${STATS_WINDOW_LABELS[h]} · ${appliedWindowHours}h`
                : STATS_WINDOW_LABELS[h],
          }))}
          onChange={(v) => onWindowChange(Number(v) as StatsWindowHours)}
          ariaLabel={t("statisticsWindow")}
          className="bg-transparent text-[10px] text-muted-foreground hover:text-foreground rounded px-1 py-0.5 cursor-pointer focus:outline-none"
        />
      </div>
      <div className="grid grid-cols-2 gap-1 px-3 py-2">
        {cards.map((card) =>
          card.kind === "warnings" ? (
            <WarningErrorCard
              key="warnings"
              stats={windowMismatch ? undefined : stats}
              placeholder={windowedPlaceholder}
              placeholderClass={placeholderClass}
              valueClass={valueClass}
              firstLoad={firstLoad}
              title={windowMismatch ? card.title : undefined}
              expanded={alertsOpen}
              onToggle={() => setAlertsOpen((open) => !open)}
            />
          ) : (
            <div
              key={card.label}
              title={windowMismatch && card.windowed ? card.title : undefined}
              className={cn("gap-0.5 px-2 py-1.5 rounded bg-sidebar-accent/40", FLEX, FLEX_COL)}
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
      {alertsOpen ? <AlertClassList windowHours={windowHours} /> : null}
      {pluginCards.length > 0 ? (
        // Plugin-declared cards (`contributions.ui.stats` + the values the
        // dashboard carried). Not windowed: a plugin value is a point in
        // time, so the window selector deliberately does not apply — the
        // section sits below the windowed grid precisely to say so.
        <div className="divide-y divide-border/60 border-t border-border px-3">
          {pluginCards.map((card) => (
            <PluginStatCard key={card.key} card={card} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

// ── Plugin stat card (task #2911) ──

// One declared plugin row: label + the plugin's primary value + optional
// supporting text. Values and details wrap, preserving provider line breaks
// so reset schedules remain readable without a hover. The empty state ("—")
// means the card is declared but no value
// row exists yet (no credential, or no refresh has run); `stale` dims a value
// whose last write is old, so a stopped refresh cannot pass for a fresh one —
// the age itself rides the tooltip.
function PluginStatCard({ card }: { card: PluginStatCardModel }) {
  const t = useTranslations("sidebar");
  const age = card.updatedAt !== null ? formatRelativeTime(card.updatedAt) : null;
  const title =
    card.value === null
      ? t("pluginStatNoData")
      : age !== null
        ? t("pluginStatUpdated", { ago: age })
        : undefined;
  return (
    <div
      title={title}
      className={cn(
        "gap-1 py-2.5",
        MIN_W_0,
        FLEX,
        FLEX_COL,
        card.stale && "opacity-60",
      )}
    >
      <span className="break-words text-xs font-medium text-muted-foreground">
        {card.label}
      </span>
      <span
        className={cn(
          "whitespace-pre-line break-words text-sm font-medium leading-5 tabular-nums",
          card.status === "error" && "text-destructive",
          card.status === "warn" && "text-amber-600 dark:text-amber-400",
        )}
      >
        {card.value ?? "—"}
      </span>
      {card.detail ? (
        <span className="whitespace-pre-line break-words text-xs leading-5 text-muted-foreground">
          {card.detail}
        </span>
      ) : null}
    </div>
  );
}

// ── Sidebar footer: fixed bottom strip (user ruling 2026-08-05) ──
//
// The spot an app's avatar row would occupy: Statistics (a chart icon that
// opens a small popover panel) on the left, and the four nav shortcuts
// (Memory Graph / Fleet / Insights / Control) on the right. These moved here
// from the header bar; the collapsed rail keeps icon-only versions.

export function SidebarFooter({ activeAgentId }: { activeAgentId: number | null }) {
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
            className={STATS_POPOVER_CLASS}
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
        <SidebarNavButton
          onClick={() => router.push(fleetHref(activeAgentId))}
          label={navT("fleet")}
        >
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

// ── Warning / Error card ──
//
// One number: the active warning/error classes of the window (an error-tracker grouping, not a
// raw event count), with the window's event total as secondary text. The card is the toggle of
// the class list below the grid (`AlertClassList`): per-class count, first/last occurrence,
// samples and dismiss / reopen. `stats` is passed undefined during a window transition so the
// card shows the same "…" placeholder as the other cards instead of a previous window's numbers.
function WarningErrorCard({
  stats,
  placeholder,
  placeholderClass,
  valueClass,
  firstLoad,
  title,
  expanded,
  onToggle,
}: {
  stats: StatsDashboard | undefined;
  placeholder: string;
  placeholderClass: string;
  valueClass: string;
  firstLoad: boolean;
  title: string | undefined;
  expanded: boolean;
  onToggle: () => void;
}) {
  const t = useTranslations("sidebar");
  const Chevron = expanded ? ChevronDown : ChevronRight;
  return (
    <button
      type="button"
      title={title}
      aria-expanded={expanded}
      onClick={onToggle}
      className={cn(
        "gap-0.5 rounded bg-sidebar-accent/40 px-2 py-1.5 text-left hover:bg-sidebar-accent/60",
        FLEX,
        FLEX_COL,
      )}
    >
      <span className={cn("items-center justify-between text-[10px] tracking-wide text-muted-foreground", FLEX)}>
        {t("warningsErrors")}
        <Chevron className="size-3" aria-hidden />
      </span>
      {firstLoad ? (
        <span
          className="h-4 w-10 animate-pulse rounded bg-muted-foreground/20"
          aria-hidden
        />
      ) : stats === undefined ? (
        <span className={placeholderClass}>{placeholder}</span>
      ) : (
        <span className={cn("items-baseline gap-1.5", FLEX)}>
          <span className={valueClass}>{stats.alert_classes_active}</span>
          <span className="truncate text-[10px] text-muted-foreground">
            {t("alertEventsCount", { count: (stats.warnings + stats.errors).toLocaleString("en-US") })}
          </span>
        </span>
      )}
    </button>
  );
}

// Placeholder row for an in-flight spawn. The row itself is direct feedback to
// a user-initiated click, but its *motion* (pulse dot + spinner) counts as a
// dynamic signal and follows the status-color opt-in: quiet mode renders a
