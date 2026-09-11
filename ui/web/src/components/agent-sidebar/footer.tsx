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

import { PluginNavIcons } from "@/components/plugin-nav";
import { WindowSelect } from "@/components/window-select";
import { errMsg as formatErrMsg } from "@/lib/errors";
import { formatTokensCompact } from "@/lib/format-number";
import {
  formatRelativeTime,
  STATS_WINDOW_LABELS,
  STATS_WINDOWS,
  useStatsDashboard,
  useStatsWindow,
  type StatsWindowHours,
} from "@/lib/sidebar";
import {
  usePluginStatCards,
  type PluginStatCard as PluginStatCardModel,
} from "@/lib/plugin-stats";
import type { StatsDashboard } from "@/lib/types";
import { FLEX, FLEX_COL } from "@/lib/layout";
import { cn } from "@/lib/utils";

import { fleetHref } from "./links";

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
      {pluginCards.length > 0 ? (
        // Plugin-declared cards (`contributions.ui.stats` + the values the
        // dashboard carried). Not windowed: a plugin value is a point in
        // time, so the window selector deliberately does not apply — the
        // section sits below the windowed grid precisely to say so.
        <div className="border-t border-border px-3 py-2">
          <div className="grid grid-cols-2 gap-1">
            {pluginCards.map((card) => (
              <PluginStatCard key={card.key} card={card} />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

// ── Plugin stat card (task #2911) ──

// One declared plugin card: label + the plugin's own value text + an optional
// detail line. The empty state ("—") means the card is declared but no value
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
        "gap-0.5 px-2 py-1.5 rounded bg-sidebar-accent/40",
        FLEX,
        FLEX_COL,
        card.stale && "opacity-60",
      )}
    >
      <span className="truncate text-[10px] tracking-wide text-muted-foreground">
        {card.label}
      </span>
      <span
        className={cn(
          "font-mono tabular-nums text-sm",
          card.status === "error" && "text-destructive",
          card.status === "warn" && "text-amber-600 dark:text-amber-400",
        )}
      >
        {card.value ?? "—"}
      </span>
      {card.detail ? (
        <span className="truncate text-[10px] text-muted-foreground" title={card.detail}>
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

// ── Warning / Error unresolved card (ruling 2026-08-29, original card
// layout restored per user feedback 2026-08-30) ──
//
// One unresolved number per level — "N / M" (warnings_net / errors_net) —
// rendered like the other five cards: a small single-cell card in the 2×3
// grid (the v0 layout from the initial public release). The full total /
// resolved / net split lives on the Grafana tiles. Zero levels render as
// plain 0 (no all-clear badge — user ruling 2026-08-30). `stats` is passed
// undefined during a window transition so the card shows the same "…"
// placeholder as the other cards instead of displaying a previous window's
// numbers.
function WarningErrorCard({
  stats,
  placeholder,
  placeholderClass,
  valueClass,
  firstLoad,
  title,
}: {
  stats: StatsDashboard | undefined;
  placeholder: string;
  placeholderClass: string;
  valueClass: string;
  firstLoad: boolean;
  title: string | undefined;
}) {
  const t = useTranslations("sidebar");
  const value =
    stats === undefined ? null : `${stats.warnings_net} / ${stats.errors_net}`;
  return (
    <div
      title={title}
      className={cn("gap-0.5 rounded bg-sidebar-accent/40 px-2 py-1.5", FLEX, FLEX_COL)}
    >
      <span className="text-[10px] tracking-wide text-muted-foreground">
        {t("warningsErrors")}
      </span>
      {firstLoad ? (
        <span
          className="h-4 w-10 animate-pulse rounded bg-muted-foreground/20"
          aria-hidden
        />
      ) : value === null ? (
        <span className={placeholderClass}>{placeholder}</span>
      ) : (
        <span className={valueClass}>{value}</span>
      )}
    </div>
  );
}

// Placeholder row for an in-flight spawn. The row itself is direct feedback to
// a user-initiated click, but its *motion* (pulse dot + spinner) counts as a
// dynamic signal and follows the status-color opt-in: quiet mode renders a
