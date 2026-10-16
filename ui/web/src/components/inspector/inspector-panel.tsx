"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bell,
  DollarSign,
  ExternalLink,
  HeartPulse,
  LayoutPanelTop,
  RefreshCw,
  SlidersHorizontal,
  Terminal,
  X,
} from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { Fragment, useCallback, useRef, type ReactNode, useEffect } from "react";

import { LiveSectionsSkeleton, SectionSkeleton } from "@/components/inspector/inspector-panel-skeleton";
import { InspectWidgetSection } from "@/components/inspector/inspector-widgets";
import { Metric, Section } from "@/components/inspector/inspector-section";
import { ScrollArea } from "@/components/ui/scroll-area";
import { OpenNoticeDetail } from "@/components/notifications/open-notice-detail";
import { WindowSelect } from "@/components/agents/window-select";
import { api } from "@/lib/transport/api";
import { formatTokens } from "@/lib/format/format-number";
import { useNow } from "@/lib/format/use-now";
import { useBreakpoint } from "@/lib/layout/breakpoint";
import { useAgentPages } from "@/lib/agents/use-agent-pages";
import { useAgentRoster } from "@/lib/agents/use-agents";
import { useInspectorHours, useInspectorOpen } from "@/lib/inspector/inspector-panel-store";
import {
  fetchWindowedInspect,
  inspectLiveQueryKey,
  inspectWidgetsQueryKey,
  inspectWindowedQueryKey,
} from "@/lib/inspector/inspector-queries";
import { INSPECTOR_RETENTION_MS } from "@/lib/layout/switch-budget";
import { fleetNoticeHref, INSPECT_SECTION_ORDER } from "@/lib/inspector/inspector-widgets";
import type {
  AgentInspectStatistics,
  AgentInspectLive,
  HeartbeatInfo,
  OpenNotice,
  PageRow,
  ShellInfo,
  SystemEvent,
} from "@/lib/contracts/types";
import { formatAbsolute, formatRelative, formatShort, formatUptime } from "@/lib/format/time";
import { useEventStream } from "@/lib/transport/useEventStream";
import { cn } from "@/lib/format/utils";
import { BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS, FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";

const WINDOWS: { labelKey: string; value: number | null }[] = [
  { labelKey: "windowAll", value: null },
  { labelKey: "window5m", value: 0 },
  { labelKey: "window1h", value: 1 },
  { labelKey: "window24h", value: 24 },
  { labelKey: "window7d", value: 168 },
];

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
  data: AgentInspectStatistics | undefined,
  agentId: number,
  hours: number | null,
): data is AgentInspectStatistics {
  if (data?.agent_id !== agentId) return false;
  return (data.window_hours ?? null) === hours;
}

export function InspectorPanel({ agentId, embedded = false }: { agentId: number; embedded?: boolean }) {
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
    staleTime: 0,
    gcTime: INSPECTOR_RETENTION_MS,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: open ? 60_000 : false,
    refetchOnMount: "always",
  });
  const windowedQuery = useQuery({
    queryKey: inspectWindowedQueryKey(agentId, hours),
    queryFn: ({ signal }) => fetchWindowedInspect(agentId, hours, signal),
    enabled: open,
    retry: false,
    staleTime: 0,
    gcTime: INSPECTOR_RETENTION_MS,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: open ? 60_000 : false,
    refetchOnMount: "always",
  });
  const widgetsQuery = useQuery({
    queryKey: inspectWidgetsQueryKey(agentId),
    queryFn: ({ signal }) => api.getAgentInspectWidgets(agentId, signal),
    enabled: open,
    retry: false,
    staleTime: INSPECTOR_RETENTION_MS,
    gcTime: INSPECTOR_RETENTION_MS,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: open ? 60_000 : false,
  });

  const liveData =
    liveQuery.data?.agent_id === agentId ? liveQuery.data : undefined;
  const windowedData = matchesInspectWindow(windowedQuery.data, agentId, hours)
    ? windowedQuery.data
    : undefined;
  const hasStaleError =
    (liveQuery.error !== null && liveData !== undefined) ||
    (windowedQuery.error !== null && windowedData !== undefined) ||
    (widgetsQuery.error !== null && widgetsQuery.data !== undefined);

  // ava:ref ui/web/src/docs/frontend-components/inspector/queries-and-refresh.ava.okf.md#cost-activity-controls
  const refreshCostActivity = useCallback(() => {
    void windowedQuery.refetch();
  }, [windowedQuery]);

  useEffect(() => {
    if (!open) {
      void queryClient.cancelQueries({ queryKey: inspectLiveQueryKey(agentId) });
      void queryClient.cancelQueries({ queryKey: ["agent-inspect", agentId] });
      void queryClient.cancelQueries({ queryKey: inspectWidgetsQueryKey(agentId) });
    }
  }, [agentId, open, queryClient]);

  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const viewport = scrollRef.current?.querySelector<HTMLDivElement>(
      '[data-slot="scroll-area-viewport"]',
    );
    if (viewport) viewport.scrollTop = 0;
  }, [agentId]);

  const pages = useAgentPages(agentId);

  const onSystemEvent = useCallback(
    (ev: SystemEvent) => {
      if (!open || ev.agent_id !== agentId) return;
      if (ev.role === "task_created" || ev.role === "task_updated") {
        void queryClient.invalidateQueries({ queryKey: inspectWidgetsQueryKey(agentId) });
      }
    },
    [agentId, open, queryClient],
  );
  const onConnectionEvent = useCallback(
    (event: { type: string }) => {
      if (open && event.type === "open") {
        void queryClient.invalidateQueries({ queryKey: inspectLiveQueryKey(agentId) });
        void queryClient.invalidateQueries({ queryKey: inspectWidgetsQueryKey(agentId) });
      }
    },
    [agentId, open, queryClient],
  );
  useEventStream(onSystemEvent, onConnectionEvent);

  if (!open) return null;

  // ava:ref ui/web/src/docs/frontend-components/inspector/inspector.ava.okf.md#section-order
  const sections: { order: number; tie: number; key: string; node: ReactNode }[] = [];
  const runLinkNode = (
    <div className={cn("items-center gap-2", FLEX)}>
      <Link
        href={`/insights/run/${agentId}`}
        className="inline-flex items-center gap-1 font-mono text-[11px] text-muted-foreground underline underline-offset-2 hover:text-foreground"
      >
        {t("openRunTimeline")}
        <ExternalLink className="size-3" aria-hidden />
      </Link>
      {hasStaleError ? <span className="ml-auto"><StaleDot /></span> : null}
    </div>
  );
  sections.push({
    order: INSPECT_SECTION_ORDER.runLink,
    tie: 0,
    key: "run-link",
    node: runLinkNode,
  });
  sections.push({
    order: INSPECT_SECTION_ORDER.costActivity,
    tie: 0,
    key: "cost-activity",
    node: (
      <CostActivitySection
        inspect={windowedData}
        failed={windowedQuery.error != null}
        pending={windowedQuery.isPending}
        hours={hours}
        onHoursChange={setHours}
        fetching={windowedQuery.isFetching}
        onRefresh={refreshCostActivity}
      />
    ),
  });
  sections.push({ order: INSPECT_SECTION_ORDER.page, tie: 0, key: "page", node: <PageSection pages={pages} /> });
  if (liveData) {
    sections.push(
      { order: INSPECT_SECTION_ORDER.shells, tie: 0, key: "shells", node: <ShellsSection inspect={liveData} /> },
      { order: INSPECT_SECTION_ORDER.liveness, tie: 0, key: "liveness", node: <LivenessSection inspect={liveData} /> },
      { order: INSPECT_SECTION_ORDER.configOverlay, tie: 0, key: "config-overlay", node: <ConfigOverlaySection inspect={liveData} /> },
    );
  } else {
    sections.push({
      order: INSPECT_SECTION_ORDER.shells,
      tie: 0,
      key: "live-state",
      node: !liveQuery.isPending ? (
        <InspectReadError
          message={liveQuery.error instanceof Error ? liveQuery.error.message : t("noData")}
          retryLabel={t("retryInspector")}
          pending={liveQuery.isFetching}
          onRetry={() => void liveQuery.refetch()}
        />
      ) : <LiveSectionsSkeleton />,
    });
  }
  if (liveData?.notice && !embedded) {
    sections.push({
      order: INSPECT_SECTION_ORDER.notice,
      tie: 0,
      key: "notice",
      node: <NoticeReplySection agentId={agentId} notice={liveData.notice} />,
    });
  }
  if (widgetsQuery.isPending || (widgetsQuery.error && !widgetsQuery.data)) {
    sections.push({
      order: INSPECT_SECTION_ORDER.page,
      tie: 1,
      key: "widgets-state",
      node: widgetsQuery.error ? (
        <InspectReadError
          message={t("widgetsUnavailable")}
          retryLabel={t("retryWidgets")}
          pending={widgetsQuery.isFetching}
          onRetry={() => void widgetsQuery.refetch()}
        />
      ) : <SectionSkeleton title={t("sectionWidgets")} rows={1} />,
    });
  }
  for (const widget of widgetsQuery.data ?? []) {
    sections.push({
      order: widget.order,
      tie: 1,
      key: `widget:${widget.plugin}/${widget.id}`,
      node: <InspectWidgetSection widget={widget} />,
    });
  }
  sections.sort(
    (a, b) => a.order - b.order || a.tie - b.tie || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0),
  );

  const body = (
    <>
      {embedded ? null : (
        <header className={cn("relative items-center gap-2 px-4", BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS, FLEX)}>
          {!isLarge ? (
            <button
              type="button"
              onClick={toggle}
              aria-label={t("closeInspector")}
              className="shrink-0 rounded p-1 -ml-1 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
            >
              <X className="size-5" />
            </button>
          ) : null}
          <span className={cn("truncate font-mono text-xs tracking-wide text-muted-foreground", MIN_W_0, FLEX_1)}>
            {t("title")}
          </span>
        </header>
      )}

      <ScrollArea ref={scrollRef} className={cn("text-xs", MIN_H_0, FLEX_1)}>
        <div className="px-4 py-3">
          <div className="space-y-4">
            {sections.map((section) => (
              <Fragment key={section.key}>{section.node}</Fragment>
            ))}
          </div>
        </div>
      </ScrollArea>
    </>
  );

  if (isLarge || embedded) {
    return (
      <aside className={cn("h-full w-full bg-background", FLEX, FLEX_COL, MIN_H_0)}>
        {body}
      </aside>
    );
  }

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

function InspectReadError({ message, retryLabel, pending, onRetry }: {
  message: string;
  retryLabel: string;
  pending: boolean;
  onRetry: () => void;
}) {
  return (
    <div className="space-y-2 font-mono text-[11px] text-destructive" role="alert">
      <p>{message}</p>
      <button
        type="button"
        onClick={onRetry}
        disabled={pending}
        className="rounded border border-destructive/40 px-2 py-1 hover:bg-destructive/10 disabled:opacity-50"
      >
        {retryLabel}
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
    <Section
      icon={<Bell className="size-3" />}
      title={t("sectionNotice")}
      action={
        <Link
          href={fleetNoticeHref(notice.id)}
          className="inline-flex items-center gap-1 hover:text-foreground"
        >
          <ExternalLink className="size-3" aria-hidden />
          {t("jumpNotice")}
        </Link>
      }
    >
      <OpenNoticeDetail
        key={notice.id}
        agentId={agentId}
        notice={notice}
        showTimestamp
        onResolved={() => {
          void queryClient.invalidateQueries({ queryKey: inspectLiveQueryKey(agentId) });
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

const SKILL_LIST_KEYS = new Set([
  "skills_to_inject_into_system_prompt",
  "skills_to_expand_at_start",
]);

function displaySkillName(name: string): string {
  return name === "*" ? name : name.replace(/_/g, "-");
}

function ConfigOverlaySection({ inspect }: { inspect: AgentInspectLive }) {
  const entries = Object.entries(inspect.config_overlay);
  const presetName = inspect.preset_name ?? null;
  const t = useTranslations("inspector");
  const { data: presetsData } = useQuery({
    queryKey: ["presets"],
    queryFn: () => api.listPresets(),
    staleTime: 60_000,
  });
  const preset = presetsData?.find((p) => p.name === presetName) ?? null;
  if (entries.length === 0 && presetName === null) return null;
  const visibleEntries =
    presetName !== null && preset
      ? entries.filter(
          ([k, v]) =>
            !(k in preset.config) ||
            JSON.stringify(preset.config[k]) !== JSON.stringify(v),
        )
      : entries;
  const presetRow = (key: string) => (
    <div key={key} className="rounded bg-sidebar-accent/40 px-2 py-1 font-mono text-[11px]">
      <dt className="break-all text-muted-foreground">{t("preset")}</dt>
      <dd className="mt-0.5 break-all text-foreground">{presetName}</dd>
    </div>
  );

  return (
    <Section icon={<SlidersHorizontal className="size-3" />} title={t("sectionConfigOverlay")}>
      <dl className="space-y-1">
        {presetName !== null && presetRow("preset")}
        {visibleEntries.map(([k, v]) => (
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

function CostActivitySection({
  inspect,
  failed,
  pending,
  hours,
  onHoursChange,
  fetching,
  onRefresh,
}: {
  inspect: AgentInspectStatistics | undefined;
  failed: boolean;
  pending: boolean;
  hours: number | null;
  onHoursChange: (hours: number | null) => void;
  fetching: boolean;
  onRefresh: () => void;
}) {
  const t = useTranslations("inspector");
  const cost = inspect?.cost;
  const activity = inspect?.activity ?? null;
  const tps = inspect?.tps;
  const hasLife = activity !== null && activity.alive_seconds > 0;
  const idleSeconds = activity
    ? Math.max(0, activity.alive_seconds - activity.active_seconds)
    : 0;
  const unpricedSub =
    cost && cost.unpriced_calls > 0
      ? t("unpriced", { count: String(cost.unpriced_calls) })
      : undefined;
  return (
    <Section
      icon={<DollarSign className="size-3" />}
      title={t("sectionCostActivity")}
      action={
        <span className={cn("shrink-0 items-center gap-1", FLEX)}>
          <button
            type="button"
            onClick={onRefresh}
            disabled={fetching}
            aria-label={t("refreshCostActivity")}
            className="rounded p-1 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={cn("size-3.5", fetching && "animate-spin")} />
          </button>
          <WindowSelect
            value={hours == null ? "all" : String(hours)}
            options={WINDOWS.map((w) => {
              const label = t(w.labelKey as Parameters<typeof t>[0]);
              const appliedHours = inspect?.applied_window_hours;
              return {
                value: String(w.value ?? "all"),
                label:
                  w.value === hours &&
                  appliedHours != null &&
                  w.value != null &&
                  appliedHours < w.value
                    ? `${label} · ${appliedHours}h`
                    : label,
              };
            })}
            onChange={(v) => onHoursChange(v === "all" ? null : Number(v))}
            ariaLabel={t("windowAriaLabel")}
            className="cursor-pointer rounded border border-border bg-transparent px-1 py-0.5 text-[10px] text-muted-foreground hover:text-foreground focus:ring-1 focus:ring-ring focus:outline-none"
          />
        </span>
      }
    >
      {inspect ? (
        <div className="space-y-1">
          {cost ? (
            <div className="grid grid-cols-2 gap-1">
              <Metric
                label={t("metricCost")}
                value={`$${cost.cost_usd.toFixed(4)}`}
                sub={unpricedSub}
              />
              <Metric label={t("metricLlmCalls")} value={String(cost.llm_calls)} />
              <Metric
                label={t("metricTokens")}
                value={`${formatTokens(cost.tokens_in)} / ${formatTokens(cost.tokens_out)}`}
              />
              <Metric
                label={t("metricCacheHit")}
                value={cost.cache_hit_pct === null ? "N/A" : `${cost.cache_hit_pct.toFixed(2)}%`}
              />
            </div>
          ) : null}
          <div className="grid grid-cols-2 gap-1">
            <Metric
              label={t("metricTps")}
              value={tps?.lm_stage_tps != null ? formatTps(tps.lm_stage_tps) : "—"}
            />
            <Metric
              label={t("metricLlmOutput")}
              value={
                hasLife && activity.llm_seconds != null
                  ? formatInterval(Math.round(activity.llm_seconds))
                  : "—"
              }
            />
            <Metric
              label={t("metricCodeExecution")}
              value={hasLife ? formatInterval(Math.round(activity.exec_seconds)) : "—"}
            />
            <Metric
              label={t("metricIdle")}
              value={hasLife ? formatInterval(Math.round(idleSeconds)) : "—"}
            />
          </div>
        </div>
      ) : failed || !pending ? (
        <InspectReadError
          message={t("windowedUnavailable")}
          retryLabel={t("retryWindowed")}
          pending={fetching}
          onRetry={onRefresh}
        />
      ) : (
        <SectionSkeleton title={t("sectionCostActivity")} rows={4} />
      )}
    </Section>
  );
}

const STATUS_LABEL_KEY: Record<string, string> = {
  running: "statusRunning",
  idling: "statusIdling",
  impersonated: "statusImpersonated",
  terminated: "statusTerminated",
};

function LivenessSection({ inspect }: { inspect: AgentInspectLive }) {
  const { liveness_state: state, heartbeat, spawned_at } = inspect;
  const { data: roster } = useAgentRoster();
  const offline = state === "offline";
  const t = useTranslations("inspector");
  const tStatus = useTranslations("agentRow");
  const next = nextHeartbeatCell(heartbeat, {
    pending: t("pending"),
    due: t("due"),
  });
  const lastPause = heartbeat.last_pause;
  const impersonated =
    inspect.status !== "terminated" &&
    roster?.agents.find((agent) => agent.agent_id === inspect.agent_id)?.status === "impersonated";
  const status = impersonated ? "impersonated" : inspect.status;
  const statusKey = STATUS_LABEL_KEY[status];
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
        <Metric label={t("metricMachine")} value={inspect.machine} />
        <Metric
          label={t("metricStatus")}
          value={statusKey ? tStatus(statusKey as Parameters<typeof tStatus>[0]) : status}
        />
      </div>
    </Section>
  );
}

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

function formatTps(n: number): string {
  if (n === 0) return "—";
  return n.toFixed(1);
}

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
