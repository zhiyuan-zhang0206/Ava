"use client";

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bell,
  ChevronLeft,
  ChevronRight,
  DollarSign,
  ExternalLink,
  HeartPulse,
  LayoutPanelTop,
  RefreshCw,
  SlidersHorizontal,
  Terminal,
  Timer,
  X,
} from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useCallback, type ReactNode, useEffect, useRef } from "react";

import { OpenNoticeDetail } from "@/components/open-notice-detail";
import { WindowSelect } from "@/components/window-select";
import { api } from "@/lib/api";
import { useNow } from "@/lib/use-now";
import { useBreakpoint } from "@/lib/breakpoint";
import { useAgentPages } from "@/lib/use-agent-pages";
import { useInspectorHours, useInspectorOpen } from "@/lib/inspector-panel-store";
import {
  COMPACT_INSPECT_WINDOW,
  fetchWindowedInspect,
  inspectLiveQueryKey,
  inspectWindowedQueryKey,
} from "@/lib/inspector-prefetch";
import type {
  AgentInspect,
  AgentInspectLive,
  HeartbeatInfo,
  OpenNotice,
  PageRow,
  ShellInfo,
  SystemEvent,
} from "@/lib/types";
import { formatAbsolute, formatRelative, formatShort, formatUptime } from "@/lib/time";
import { useEventStream } from "@/lib/useEventStream";
import { cn } from "@/lib/utils";
import { BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS, FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout";

// Window options for the cost + activity sections. `null` = cumulative since
// spawn (available as All); 0 = the last 5m and the positive values are a subset
// of the backend whitelist (StatsWindowHours: 1/6/24/72/168 = hours). -1 is
// a local sentinel for the since-last-compact window — it never reaches the
// backend as `hours`, instead selecting the `since_compact` query param.
const WINDOWS: { labelKey: string; value: number | null }[] = [
  { labelKey: "windowAll", value: null },
  { labelKey: "window5m", value: 0 },
  { labelKey: "window1h", value: 1 },
  { labelKey: "window24h", value: 24 },
  { labelKey: "window7d", value: 168 },
  { labelKey: "windowSinceCompact", value: COMPACT_INSPECT_WINDOW },
];

/**
 * Right-side inspector panel for the active agent — the single-agent
 * counterpart to the sidebar's fleet-wide stats card. Sections include
 * persistent shells, the frozen config overlay, and LLM cost.
 *
 * Fetch discipline: the uncached live query supplies shells/liveness/config/
 * notice independently of the slower windowed aggregate query. Both load on
 * open, refresh manually or every 60s, and cancel on close. While the panel is
 * open, sidebar-row intent can prefetch these same keys; closed stays at zero
 * inspect traffic. Notice SSE invalidates both halves. Window transitions use
 * per-section skeletons instead of displaying a previous window's totals.
 *
 * Responsive (user ruling 2026-08-23, superseding the 2026-08-05 floating
 * overlay ruling on desktop): at ≥ lg it fills a resizable right-side panel;
 * below lg it is a full-screen overlay with a backdrop, matching the mobile
 * sidebar drawer. The header X closes both forms and the backdrop closes the
 * mobile overlay; Escape deliberately does not close either form (user ruling
 * 2026-08-24).
 */
// A subtle "live refresh is failing" marker for the inspector header. Shown only
// when we already have a snapshot to display (stale-while-error) — a cold failure
// gets the full error message in the body instead, so this never replaces content.
function StaleDot() {
  const t = useTranslations("inspector");
  return (
    <span
      aria-label={t("liveRefreshFailing")}
      className="size-1.5 shrink-0 rounded-full bg-amber-500"
    />
  );
}

function matchesInspectWindow(
  data: AgentInspect | undefined,
  agentId: number,
  hours: number | null,
): data is AgentInspect {
  if (data?.agent_id !== agentId) return false;
  if (hours === COMPACT_INSPECT_WINDOW) return data.since_compact;
  return !data.since_compact && (data.window_hours ?? null) === hours;
}

export function InspectorPanel({ agentId }: { agentId: number }) {
  const t = useTranslations("inspector");
  const { open, toggle } = useInspectorOpen();
  const { inspectorHours: hours, setInspectorHours: setHours } = useInspectorHours();
  const { isLarge } = useBreakpoint();
  const queryClient = useQueryClient();

  const liveQuery = useQuery({
    queryKey: inspectLiveQueryKey(agentId),
    queryFn: ({ signal }) => api.getAgentInspectLive(agentId, signal),
    enabled: open,
    retry: false,
    refetchInterval: open ? 60_000 : false,
    refetchOnMount: "always",
  });
  const windowedQuery = useQuery({
    queryKey: inspectWindowedQueryKey(agentId, hours),
    queryFn: ({ signal }) => fetchWindowedInspect(agentId, hours, signal),
    enabled: open,
    retry: false,
    refetchInterval: open ? 60_000 : false,
    refetchOnMount: "always",
    placeholderData: keepPreviousData,
  });

  // Both query keys include the agent id, but keep explicit response identity
  // guards: a malformed/misrouted response must never render under another
  // agent. The aggregate response also has to echo the selected window; during
  // keepPreviousData transitions a prior window becomes section skeletons,
  // never mislabeled numbers.
  const liveData =
    liveQuery.data?.agent_id === agentId ? liveQuery.data : undefined;
  const windowedData = matchesInspectWindow(windowedQuery.data, agentId, hours)
    ? windowedQuery.data
    : undefined;
  const isFetching = liveQuery.isFetching || windowedQuery.isFetching;
  const hasStaleError =
    (liveQuery.error !== null && liveData !== undefined) ||
    (windowedQuery.error !== null && windowedData !== undefined);

  const refresh = useCallback(() => {
    void liveQuery.refetch();
    void windowedQuery.refetch();
  }, [liveQuery, windowedQuery]);

  // Disabling an observer does not itself guarantee transport cancellation.
  // Consume React Query's AbortSignal above and explicitly cancel when the
  // panel closes so a hidden inspector cannot leave its expensive fan-out
  // running in the gateway.
  useEffect(() => {
    if (!open) {
      void queryClient.cancelQueries({ queryKey: inspectLiveQueryKey(agentId) });
      void queryClient.cancelQueries({ queryKey: ["agent-inspect", agentId] });
    }
  }, [agentId, open, queryClient]);

  // Open pages: SSE-driven cache (page_opened/page_closed fold in live), not a
  // poll — see useAgentPages.
  const pages = useAgentPages(agentId);

  const invalidateInspect = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: inspectLiveQueryKey(agentId) });
    void queryClient.invalidateQueries({ queryKey: ["agent-inspect", agentId] });
  }, [agentId, queryClient]);

  // Agent switch must show the NEW agent's data immediately (task #1939).
  // The panel is mounted only while open, so a switch with the panel open
  // re-keys the inspect queries on an already-mounted observer — and TanStack
  // only refetches that path when the cache is stale (refetchOnMount: "always"
  // applies to a true mount only, i.e. opening the panel). With the app's
  // global 5min staleTime a hot switch-back therefore keeps the previous
  // visit's cached numbers on screen until the next 60s interval tick.
  // Invalidate on agentId change while open to force the background refresh
  // (the first run is skipped — the fresh mount fetches on its own); a cold
  // key is a no-op and an in-flight fetch is deduped by the query cache.
  const firstAgentIdRef = useRef(agentId);
  useEffect(() => {
    if (firstAgentIdRef.current === agentId) return;
    firstAgentIdRef.current = agentId;
    invalidateInspect();
  }, [agentId, invalidateInspect]);

  // Notice events affect the live notice immediately and may coincide with
  // aggregate activity, so reconcile both halves of the inspector cache.
  const onSystemEvent = useCallback(
    (ev: SystemEvent) => {
      if (ev.agent_id !== agentId) return;
      if (ev.role === "notice_posted" || ev.role === "notice_resolved") {
        invalidateInspect();
      }
    },
    [agentId, invalidateInspect],
  );
  const onConnectionEvent = useCallback(
    (_ev: { type: string }) => {
      // On reconnect, reconcile the notice state (may have changed while disconnected).
      if (_ev.type === "open") {
        invalidateInspect();
      }
    },
    [invalidateInspect],
  );
  useEventStream(onSystemEvent, onConnectionEvent);

  // Renders nothing while closed, so desktop releases the flex-column width
  // and mobile removes the overlay. All hooks run regardless (rules-of-hooks),
  // but the query is disabled so a closed panel cannot produce inspect traffic.
  if (!open) return null;

  const body = (
    <>
      <header className={cn("relative items-center gap-2 px-4", BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS, FLEX)}>
        <button
          type="button"
          onClick={toggle}
          aria-label={t("closeInspector")}
          className="shrink-0 rounded p-1 -ml-1 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
        >
          <X className="size-5" />
        </button>
        <span className={cn("truncate font-mono text-xs tracking-wide text-muted-foreground", MIN_W_0, FLEX_1)}>
          {t("title")}
        </span>
        {hasStaleError ? <StaleDot /> : null}
        <button
          type="button"
          onClick={refresh}
          disabled={isFetching}
          aria-label={t("refreshInspector")}
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground disabled:opacity-50"
        >
          <RefreshCw className={cn("size-3.5", isFetching && "animate-spin")} />
        </button>
        <WindowSelect
          value={hours == null ? "all" : String(hours)}
          options={WINDOWS.map((w) => ({
            value: String(w.value ?? "all"),
            label: t(w.labelKey as Parameters<typeof t>[0]),
          }))}
          onChange={(v) => setHours(v === "all" ? null : Number(v))}
          ariaLabel={t("windowAriaLabel")}
          className="shrink-0 cursor-pointer rounded border border-border bg-transparent px-1 py-0.5 text-[10px] text-muted-foreground hover:text-foreground focus:ring-1 focus:ring-ring focus:outline-none"
        />
      </header>

      <div className={cn("overflow-y-auto px-4 py-3 text-xs", MIN_H_0, FLEX_1)}>
        {liveQuery.error && !liveData ? (
          <div className="space-y-2 font-mono text-[11px] text-destructive" role="alert">
            <p>
              {liveQuery.error instanceof Error
                ? liveQuery.error.message
                : t("loadFailed")}
            </p>
            <button
              type="button"
              onClick={refresh}
              disabled={isFetching}
              aria-label={t("retryInspector")}
              className="rounded border border-destructive/40 px-2 py-1 hover:bg-destructive/10 disabled:opacity-50"
            >
              {isFetching ? t("retrying") : t("retry")}
            </button>
          </div>
        ) : (
          <div className="space-y-4">
            <PageSection pages={pages} />
            {liveData ? (
              <>
                <ShellsSection inspect={liveData} />
                <LivenessSection inspect={liveData} />
                <ConfigOverlaySection inspect={liveData} />
              </>
            ) : liveQuery.isPending ? (
              <LiveSectionsSkeleton />
            ) : (
              <p className="font-mono text-[11px] text-muted-foreground">{t("noData")}</p>
            )}
            {windowedData ? (
              <>
                <CostSection inspect={windowedData} />
                <ActivitySection inspect={windowedData} />
              </>
            ) : windowedQuery.error ? (
              <WindowedSectionsError onRetry={() => void windowedQuery.refetch()} />
            ) : (
              <WindowedSectionsSkeleton />
            )}
            <Link
              href={`/insights/run/${agentId}`}
              className="inline-flex items-center gap-1 font-mono text-[11px] text-muted-foreground underline underline-offset-2 hover:text-foreground"
            >
              {t("openRunTimeline")}
              <ExternalLink className="size-3" aria-hidden />
            </Link>
            {liveData?.notice ? (
              <NoticeReplySection agentId={agentId} notice={liveData.notice} />
            ) : liveQuery.isPending ? (
              <SectionSkeleton title={t("sectionNotice")} />
            ) : null}
          </div>
        )}
      </div>
    </>
  );

  // Desktop: fill the parent resizable panel. The 2026-08-23 ruling supersedes
  // the 2026-08-05 floating overlay for this breakpoint.
  if (isLarge) {
    return (
      <aside className={cn("h-full w-full bg-background", FLEX, FLEX_COL, MIN_H_0)}>
        {body}
      </aside>
    );
  }

  // Mobile: full-screen overlay with backdrop (Task #793 semantics restored
  // by the 2026-08-23 ruling).
  return (
    <div className={cn("fixed inset-0 z-50", FLEX)}>
      <div
        className="absolute inset-0 bg-black/40"
        onClick={toggle}
        aria-hidden="true"
      />
      <aside className={cn("relative w-full bg-background", FLEX, FLEX_COL)}>{body}</aside>
    </div>
  );
}

/** Inspector toggle button — opens/closes the panel from the HeaderBar's
 *  children slot at the top-right of the content column. Closed means no
 *  inspect traffic: the panel's enabled query performs the first fetch only
 *  after this button opens it. */
export function InspectorToggle() {
  const { open, toggle } = useInspectorOpen();
  const t = useTranslations("inspector");
  return (
    <button
      type="button"
      onClick={toggle}
      data-inspector-toggle=""
      aria-label={open ? t("closeInspector") : t("openInspector")}
      className={cn(
        "inline-flex items-center gap-1.5 rounded border px-1.5 py-0.5 font-mono text-[11px] transition-colors select-none",
        open
          ? "border-border bg-accent text-accent-foreground"
          : "border-transparent text-muted-foreground/50 hover:border-border hover:text-muted-foreground",
      )}
    >
      {/* Closed points right to open the right-side panel; open points left to
          close it back and keeps the "Close inspector" semantics. The
          2026-08-24 user ruling supersedes the 8/6 and #1065 up-arrow ruling. */}
      {open ? <ChevronLeft className="size-3.5" /> : <ChevronRight className="size-3.5" />}
      <span className="hidden sm:inline">{t("toggle")}</span>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

function Section({
  icon,
  title,
  badge,
  children,
}: {
  icon: ReactNode;
  title: string;
  badge?: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-1.5">
      <div className={cn("items-center gap-1.5 text-[10px] tracking-wide text-muted-foreground", FLEX)}>
        {icon}
        <span>{title}</span>
        {badge != null && (
          <span className="ml-auto font-mono text-[11px] tabular-nums text-foreground normal-case">
            {badge}
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

function SectionSkeleton({ title, rows = 2 }: { title: string; rows?: number }) {
  return (
    <section aria-label={`${title} loading`} className="space-y-1.5">
      <div className="h-3 w-24 animate-pulse rounded bg-muted-foreground/20" />
      <div className="grid grid-cols-2 gap-1">
        {Array.from({ length: rows }, (_, index) => (
          <div
            key={index}
            className="h-10 animate-pulse rounded bg-muted-foreground/10"
          />
        ))}
      </div>
    </section>
  );
}

function LiveSectionsSkeleton() {
  const t = useTranslations("inspector");
  return (
    <>
      <SectionSkeleton title={t("sectionShells")} rows={1} />
      <SectionSkeleton title={t("sectionLiveness")} rows={3} />
      <SectionSkeleton title={t("sectionConfigOverlay")} rows={1} />
    </>
  );
}

function WindowedSectionsSkeleton() {
  const t = useTranslations("inspector");
  return (
    <>
      <SectionSkeleton title={t("sectionCost")} rows={4} />
      <SectionSkeleton title={t("sectionActivity")} rows={4} />
    </>
  );
}

function WindowedSectionsError({ onRetry }: { onRetry: () => void }) {
  const t = useTranslations("inspector");
  return (
    <div className="space-y-2 font-mono text-[11px] text-destructive" role="alert">
      <p>{t("windowedUnavailable")}</p>
      <button
        type="button"
        onClick={onRetry}
        className="rounded border border-destructive/40 px-2 py-1 hover:bg-destructive/10"
      >
        {t("retryWindowed")}
      </button>
    </div>
  );
}

function PageSection({ pages }: { pages: PageRow[] }) {
  const t = useTranslations("inspector");
  if (pages.length === 0) return null;

  return (
    <Section icon={<LayoutPanelTop className="size-3" />} title={t("sectionPage")}>
      {pages.map((p) => (
        <a
          key={p.name}
          href={p.url}
          target="_blank"
          rel="noopener noreferrer"
          className={cn("items-center gap-2 rounded bg-sidebar-accent/40 px-2 py-1.5 font-mono text-[11px] hover:bg-sidebar-accent group", FLEX)}
        >
          <ExternalLink className="size-3 shrink-0 text-muted-foreground group-hover:text-foreground" />
          <span className={cn(FLEX, FLEX_COL, MIN_W_0, FLEX_1)}>
            <span className="truncate text-foreground">{p.title ?? p.name}</span>
            <span
              className="truncate text-[10px] text-muted-foreground"
            >
              {p.url}
            </span>
          </span>
        </a>
      ))}
    </Section>
  );
}

// The agent's single open notice, rendered at the bottom of the panel as an
// interactive reply surface (mirrors the fleet "waiting on you" queue): a
// require_response notice gets a reply box + Dismiss, an FYI gets Mark read.
// Resolving invalidates the inspect query so the notice clears without waiting
// out the slow background interval. The parent omits this section when no
// notice exists, matching the Inspector's other empty-section rules.

function NoticeReplySection({
  agentId,
  notice,
}: {
  agentId: number;
  notice: OpenNotice;
}) {
  const queryClient = useQueryClient();
  const t = useTranslations("inspector");
  return (
    <Section icon={<Bell className="size-3" />} title={t("sectionNotice")}>
      {/* Key by notice id: OpenNoticeDetail keeps `pending` true after a resolve
          (the notice is going away), so when a refetch swaps in the next notice
          the keyed remount gives it a fresh, enabled reply surface. */}
      <OpenNoticeDetail
        key={notice.id}
        agentId={agentId}
        notice={notice}
        showTimestamp
        onResolved={() => {
          void queryClient.invalidateQueries({ queryKey: inspectLiveQueryKey(agentId) });
          void queryClient.invalidateQueries({ queryKey: ["agent-inspect", agentId] });
        }}
      />
    </Section>
  );
}

function ShellsSection({ inspect }: { inspect: AgentInspectLive }) {
  const { shells } = inspect;
  const now = useNow(1_000);
  const t = useTranslations("inspector");
  if (inspect.shells_available === true && shells.length === 0) return null;

  return (
    <Section
      icon={<Terminal className="size-3" />}
      title={t("sectionShells")}
      badge={inspect.shells_available === true ? String(shells.length) : "?"}
    >
      {inspect.shells_available !== true ? (
        <p className="font-mono text-[11px] text-muted-foreground">Shell observation unavailable</p>
      ) : (
        <ul className="space-y-1">
          {shells.map((s) => (
            <ShellRow key={s.id} agentId={inspect.agent_id} shell={s} now={now} />
          ))}
        </ul>
      )}
    </Section>
  );
}

// A shell row links to its full-screen monitor page (live terminal tail).
// Each row shows only the runtime value (launch → now, ticking live; falls
// back to the probe-time uptime snapshot when created_at is missing) — the
// user can infer the creation time from it, and the created/TTL detail lives
// on the monitor page's title bar (user corrections 2026-08-28). Guards
// against NaN / missing ids from a partial API response — if either agentId
// or shell.id is not a valid finite integer, renders as plain text (no link)
// to avoid navigating to /shell/NaN/NaN.
function ShellRow({
  agentId,
  shell,
  now,
}: {
  agentId: number;
  shell: ShellInfo;
  now: Date;
}) {
  const t = useTranslations("inspector");
  const validAgent = Number.isFinite(agentId) && agentId >= 0;
  const validShell = Number.isFinite(shell.id) && shell.id >= 0;
  const createdMs = shell.created_at != null ? new Date(shell.created_at).getTime() : NaN;
  const runtimeSeconds =
    Number.isFinite(createdMs)
      ? Math.max(0, Math.floor((now.getTime() - createdMs) / 1000))
      : shell.uptime_seconds;
  const rowClass =
    "flex items-center gap-2 rounded bg-sidebar-accent/40 px-2 py-1 font-mono text-[11px]";
  const content = (
    <>
      <span className="tabular-nums text-muted-foreground">#{shell.id}</span>
      <span className="truncate text-foreground">{shell.name ?? t("unnamed")}</span>
      <span className="ml-auto shrink-0 tabular-nums text-muted-foreground">
        {formatUptime(runtimeSeconds)}
      </span>
    </>
  );

  if (!validAgent || !validShell) {
    return (
      <li>
        <span className={rowClass}>{content}</span>
      </li>
    );
  }

  return (
    <li>
      <Link
        href={`/shell/${agentId}/${shell.id}`}
        className={`${rowClass} hover:bg-sidebar-accent`}
      >
        {content}
      </Link>
    </li>
  );
}

// Config keys whose values are skill-name lists. The agent runtime stores
// these in the underscore Python projection (ava_code_worktree); the UI
// renders the canonical dash spelling (ava-code-worktree) — the same rule the
// `# Capabilities` index and the skills panel follow
// (shared.skill_names.display_name).
const SKILL_LIST_KEYS = new Set([
  "skills_to_inject_into_system_prompt",
  "skills_to_expand_at_start",
]);

function displaySkillName(name: string): string {
  return name === "*" ? name : name.replace(/_/g, "-");
}

function ConfigOverlaySection({ inspect }: { inspect: AgentInspectLive }) {
  const entries = Object.entries(inspect.config_overlay);
  const t = useTranslations("inspector");
  if (entries.length === 0) return null;

  return (
    <Section icon={<SlidersHorizontal className="size-3" />} title={t("sectionConfigOverlay")}>
      <dl className="space-y-1">
        {entries.map(([k, v]) => (
          <div
            key={k}
            className="rounded bg-sidebar-accent/40 px-2 py-1 font-mono text-[11px]"
          >
            <dt className="break-all text-muted-foreground">{k}</dt>
            <dd className="mt-0.5 break-all text-foreground">{formatValue(SKILL_LIST_KEYS.has(k) && Array.isArray(v) ? v.map(displaySkillName) : v)}</dd>
          </div>
        ))}
      </dl>
    </Section>
  );
}

function CostSection({ inspect }: { inspect: AgentInspect }) {
  const { cost } = inspect;
  // Cost is the sum of stored usage-time price snapshots; calls without one
  // (unpriced model) contribute 0 and surface as the sub-line so the figure
  // is never silently partial.
  const t = useTranslations("inspector");
  const unpricedSub =
    cost.unpriced_calls > 0 ? t("unpriced", { count: String(cost.unpriced_calls) }) : undefined;
  return (
    <Section icon={<DollarSign className="size-3" />} title={t("sectionCost")}>
      <div className="grid grid-cols-2 gap-1">
        <Metric label={t("metricCost")} value={`$${cost.cost_usd.toFixed(4)}`} sub={unpricedSub} />
        <Metric label={t("metricLlmCalls")} value={String(cost.llm_calls)} />
        <Metric
          label={t("metricTokens")}
          value={`${formatTokens(cost.tokens_in)} / ${formatTokens(cost.tokens_out)}`}
        />
        <Metric label={t("metricCacheHit")} value={`${cost.cache_hit_pct.toFixed(2)}%`} />
      </div>
    </Section>
  );
}

/**
 * Activity — TPS plus absolute time spent in LLM reasoning,
 * code execution, and idle/blocked states. The duration cells follow the
 * header window.
 */
function ActivitySection({ inspect }: { inspect: AgentInspect }) {
  const { activity, tps } = inspect;
  const hasLife = activity.alive_seconds > 0;
  const idleSeconds = Math.max(0, activity.alive_seconds - activity.active_seconds);
  const t = useTranslations("inspector");
  return (
    <Section icon={<Timer className="size-3" />} title={t("sectionActivity")}>
      <div className="grid grid-cols-2 gap-1">
        <Metric label={t("metricTps")} value={formatTps(tps.lm_stage_tps)} />
        <Metric
          label={t("metricLlmOutput")}
          value={
            hasLife
              ? formatInterval(Math.round(activity.llm_seconds))
              : "—"
          }
        />
        <Metric
          label={t("metricCodeExecution")}
          value={
            hasLife
              ? formatInterval(Math.round(activity.exec_seconds))
              : "—"
          }
        />
        <Metric
          label={t("metricIdle")}
          value={hasLife ? formatInterval(Math.round(idleSeconds)) : "—"}
        />
      </div>
    </Section>
  );
}

/**
 * Liveness — one merged section (Task #1195, user ruling 2026-08-12) with
 * three cells: agent birth, next heartbeat, and last pause. The gateway-owned
 * derived liveness state only colors the HeartPulse icon when offline because
 * the timeline header already displays agent status. The "every N" badge and
 * old "Last judged" cell remain omitted.
 */
function LivenessSection({ inspect }: { inspect: AgentInspectLive }) {
  const { liveness_state: state, heartbeat, spawned_at } = inspect;
  const offline = state === "offline";
  const t = useTranslations("inspector");
  const next = nextHeartbeatCell(heartbeat, {
    pending: t("pending"),
    due: t("due"),
  });
  const lastPause = heartbeat.last_pause;
  return (
    <Section icon={<HeartPulse className={cn("size-3", offline && "text-destructive")} />} title={t("sectionLiveness")}>
      <div className="grid grid-cols-2 gap-1">
        <Metric
          className="col-span-2"
          label={t("metricBirth")}
          value={`${formatRelative(spawned_at)}, ${formatAbsolute(spawned_at)}`}
        />
        <Metric label={t("metricNextHeartbeat")} value={next.value} />
        <Metric
          label={t("metricLastPause")}
          value={
            lastPause
              ? `${formatRelative(lastPause.at)} · ${formatInterval(Math.round(lastPause.duration_s))}`
              : t("neverPaused")
          }
        />
      </div>
    </Section>
  );
}

// The "next heartbeat" cell — mirrors the backend's mutually-exclusive states:
// an active pause renders a clock time; an idle-family agent (idling /
// restarting — the statuses the fleet view projects to Idle)
// with a check-in already queued (the daemon won't send another while an
// inbound is pending) renders "pending"; one with nothing queued renders its
// projected next check-in, or "due" when the projection has passed (a
// restarting agent's idle clock runs on while the daemon skips it — a past
// "next" time must never render as "Xm ago"); a running or terminated agent
// an em dash (never checked in on).
function nextHeartbeatCell(
  hb: HeartbeatInfo,
  labels: { pending: string; due: string },
): { value: string } {
  if (hb.paused_until) {
    return {
      value: formatShort(hb.paused_until, { includeDate: false }),
    };
  }
  if (hb.heartbeat_pending) {
    return { value: labels.pending };
  }
  if (hb.next_at) {
    const next = new Date(hb.next_at).getTime();
    return {
      value: next <= Date.now() ? labels.due : formatRelative(hb.next_at),
    };
  }
  return { value: "—" };
}

function Metric({
  className,
  label,
  value,
  sub,
}: {
  className?: string;
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className={cn("gap-0.5 rounded bg-sidebar-accent/40 px-2 py-1", FLEX, FLEX_COL, className)}>
      <span className="text-[10px] tracking-wide text-muted-foreground">{label}</span>
      <span className="font-mono text-xs tabular-nums text-foreground">{value}</span>
      {sub != null && (
        <span className="font-mono text-[10px] tabular-nums text-muted-foreground/70">{sub}</span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function formatTokens(n: number): string {
  if (n < 1_000) return String(n);
  if (n < 1_000_000) return `${(n / 1_000).toFixed(1)}k`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n < 1_000_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
  return `${(n / 1_000_000_000_000).toFixed(2)}T`;
}

function formatTps(n: number): string {
  if (n === 0) return "—";
  return n.toFixed(1);
}

// A heartbeat interval / pause duration as a compact span: `45s` / `15m` /
// `1h 30m` / `24d 3h` (day tier kicks in past 24h).
function formatInterval(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
  const d = Math.floor(h / 24);
  const remH = h % 24;
  return remH ? `${d}d ${remH}h` : `${d}d`;
}

function formatValue(v: unknown): string {
  return typeof v === "string" ? v : JSON.stringify(v);
}
