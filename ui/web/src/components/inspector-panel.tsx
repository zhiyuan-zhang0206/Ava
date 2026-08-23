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
import Link from "next/link";
import { useCallback, type ReactNode, useEffect, useState } from "react";

import { OpenNoticeDetail } from "@/components/open-notice-detail";
import { api } from "@/lib/api";
import { useBreakpoint } from "@/lib/breakpoint";
import { useAgentPages } from "@/lib/use-agent-pages";
import { useInspectorOpen } from "@/lib/inspector-panel-store";
import type {
  AgentInspect,
  HeartbeatInfo,
  OpenNotice,
  PageRow,
  ShellInfo,
  SystemEvent,
} from "@/lib/types";
import { formatAbsolute, formatRelative, formatShort } from "@/lib/time";
import { useEventStream } from "@/lib/useEventStream";
import { cn } from "@/lib/utils";
import { BAR_HEIGHT_CLASS, FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout";

// Window options for the cost + activity sections. `null` = cumulative since
// spawn (available as All); 0 = the last 5m and the positive values are a subset
// of the backend whitelist (StatsWindowHours: 1/6/24/72/168 = hours). -1 is
// a local sentinel for the since-last-compact window — it never reaches the
// backend as `hours`, instead selecting the `since_compact` query param.
const COMPACT_WINDOW = -1;
const WINDOWS: { label: string; value: number | null }[] = [
  { label: "All", value: null },
  { label: "5m", value: 0 },
  { label: "1h", value: 1 },
  { label: "24h", value: 24 },
  { label: "7d", value: 168 },
  { label: "Since compact", value: COMPACT_WINDOW },
];

/**
 * Right-side inspector panel for the active agent — the single-agent
 * counterpart to the sidebar's fleet-wide stats card. Sections include
 * persistent shells, the frozen config overlay, and LLM cost.
 *
 * Fetch discipline (2026-08-18 incident: this endpoint's 5s poll pinned the
 * prod box — GET /api/agents/{id}/inspect runs ~25 whole-life Loki
 * aggregations plus a cross-machine shell probe per call): data loads when
 * the panel OPENS (refetchOnMount:"always"), refreshes on the header's
 * manual refresh button, and drifts on a slow 60s background interval while
 * the panel stays open. Notice SSE events invalidate immediately. The header
 * window selector re-scopes cost (default: the recent 24h window).
 *
 * Responsive (user ruling 2026-08-23, superseding the 2026-08-05 floating
 * overlay ruling on desktop): at ≥ lg it is a fixed right-side flex panel;
 * below lg it is a full-screen overlay with a backdrop, matching the mobile
 * sidebar drawer.
 */
// A subtle "live refresh is failing" marker for the inspector header. Shown only
// when we already have a snapshot to display (stale-while-error) — a cold failure
// gets the full error message in the body instead, so this never replaces content.
function StaleDot() {
  return (
    <span
      aria-label="Live refresh failing"
      className="size-1.5 shrink-0 rounded-full bg-amber-500"
    />
  );
}

export function InspectorPanel({ agentId }: { agentId: number }) {
  const { open, toggle } = useInspectorOpen();
  const { isLarge } = useBreakpoint();
  const [hours, setHours] = useState<number | null>(24);
  const queryClient = useQueryClient();

  // Responsive side panel / mobile overlay (user ruling 2026-08-23,
  // superseding the 2026-08-05 floating-panel ruling on desktop): Escape
  // closes both forms. The header X closes both; the mobile backdrop also
  // closes the overlay.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") toggle();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, toggle]);

  const { data, error, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["agent-inspect", agentId, hours],
    queryFn: ({ signal }) =>
      api.getAgentInspect(
        agentId,
        hours === COMPACT_WINDOW ? null : hours,
        hours === COMPACT_WINDOW,
        signal,
      ),
    // Slow background drift only — the endpoint is expensive (~25 Loki
    // aggregations + a shell-probe RPC per call), so the interval is a floor
    // for "numbers don't rot while the panel sits open", not the freshness
    // mechanism. Freshness comes from refetchOnMount on open, the header's
    // manual refresh, and SSE-driven invalidation (notices below).
    enabled: open,
    // The request already has explicit server/client deadlines and the cold
    // error surface owns a manual Retry action. Inheriting the global three
    // automatic retries would multiply one 15s overload into a minute-long
    // spinner and would enqueue more expensive history reads while Loki is ill.
    retry: false,
    refetchInterval: open ? 60_000 : false,
    // Fetch once on open, not just cold. The global 5min staleTime otherwise
    // treats cached inspect data from a previous open as fresh, so opening
    // the panel would show stale numbers;
    // "always" pulls immediately (cached data stays on screen meanwhile via
    // placeholderData).
    refetchOnMount: "always",
    // Changing the window (`hours` is in the key) mints a fresh cache entry
    // with no data yet; without this the whole panel — including the
    // window-independent shells/config/notice sections — would blank to
    // "loading…" on every window switch until the new fetch lands. Keep the
    // previous window's data rendered while the new one loads.
    placeholderData: keepPreviousData,
  });

  // Disabling an observer does not itself guarantee transport cancellation.
  // Consume React Query's AbortSignal above and explicitly cancel when the
  // panel closes so a hidden inspector cannot leave its expensive fan-out
  // running in the gateway.
  useEffect(() => {
    if (!open) {
      void queryClient.cancelQueries({ queryKey: ["agent-inspect", agentId] });
    }
  }, [agentId, open, queryClient]);

  // Open pages: SSE-driven cache (page_opened/page_closed fold in live), not a
  // poll — see useAgentPages.
  const pages = useAgentPages(agentId);

  // Keep the inspect query fresh on notice events — notice_posted/notice_resolved
  // SSE arrive long before the slow 60s interval, so the notice section updates
  // in real time (same pattern as useInboxFeed).
  const onSystemEvent = useCallback(
    (ev: SystemEvent) => {
      if (ev.agent_id !== agentId) return;
      if (ev.role === "notice_posted" || ev.role === "notice_resolved") {
        void queryClient.invalidateQueries({ queryKey: ["agent-inspect", agentId] });
      }
    },
    [agentId, queryClient],
  );
  const onConnectionEvent = useCallback(
    (_ev: { type: string }) => {
      // On reconnect, reconcile the notice state (may have changed while disconnected).
      if (_ev.type === "open") {
        void queryClient.invalidateQueries({ queryKey: ["agent-inspect", agentId] });
      }
    },
    [agentId, queryClient],
  );
  useEventStream(onSystemEvent, onConnectionEvent);

  // Keep the last successfully loaded data so the panel never blanks when the
  // query key resets for a same-agent window change. React Query's
  // keepPreviousData is intentionally broader than that: it can also hand the
  // observer agent A's result while agent B is pending, so both the query value
  // and our local fallback are identity-checked before either can render.
  const [lastData, setLastData] = useState<AgentInspect>();
  // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: cache last successful data to avoid blanking
  useEffect(() => { if (data?.agent_id === agentId) setLastData(data); }, [agentId, data]);
  // Cross-agent guard (Task #1051): on agent switch, drop the previous
  // agent's snapshot immediately — while the new query is in flight, `data`
  // is undefined for a COLD key and effectiveData would otherwise fall back
  // to agent A's snapshot and render it (shells/cost/notice — and the
  // NoticeReplySection reply/read writes) under agent B's name.
  // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: stale snapshot must not survive an agent switch
  useEffect(() => { setLastData(undefined); }, [agentId]);
  const matchingData = data?.agent_id === agentId ? data : undefined;
  const matchingLastData = lastData?.agent_id === agentId ? lastData : undefined;
  const effectiveData = matchingData ?? matchingLastData;

  // Renders nothing while closed, so desktop releases the flex-column width
  // and mobile removes the overlay. All hooks run regardless (rules-of-hooks),
  // but the query is disabled so a closed panel cannot produce inspect traffic.
  if (!open) return null;

  const body = (
    <>
      <header className={cn("items-center gap-2 border-b border-border px-4", BAR_HEIGHT_CLASS, FLEX)}>
        <button
          type="button"
          onClick={toggle}
          aria-label="Close inspector"
          className="shrink-0 rounded p-1 -ml-1 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
        >
          <X className="size-5" />
        </button>
        <span className={cn("truncate font-mono text-xs tracking-wide text-muted-foreground", MIN_W_0, FLEX_1)}>
          Inspector
        </span>
        {error && effectiveData ? <StaleDot /> : null}
        <button
          type="button"
          onClick={() => void refetch()}
          disabled={isFetching}
          aria-label="Refresh inspector data"
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground disabled:opacity-50"
        >
          <RefreshCw className={cn("size-3.5", isFetching && "animate-spin")} />
        </button>
        <select
          value={hours ?? "all"}
          onChange={(e) => setHours(e.target.value === "all" ? null : Number(e.target.value))}
          aria-label="Cost + activity window"
          className="shrink-0 cursor-pointer rounded border border-border bg-transparent px-1 py-0.5 text-[10px] text-muted-foreground hover:text-foreground focus:ring-1 focus:ring-ring focus:outline-none"
        >
          {WINDOWS.map((w) => (
            <option key={w.label} value={w.value ?? "all"}>
              {w.label}
            </option>
          ))}
        </select>
      </header>

      <div className={cn("overflow-y-auto px-4 py-3 text-xs", MIN_H_0, FLEX_1)}>
        {error && !effectiveData ? (
          <div className="space-y-2 font-mono text-[11px] text-destructive" role="alert">
            <p>{error instanceof Error ? error.message : "Failed to load"}</p>
            <button
              type="button"
              onClick={() => void refetch()}
              disabled={isFetching}
              aria-label="Retry inspector"
              className="rounded border border-destructive/40 px-2 py-1 hover:bg-destructive/10 disabled:opacity-50"
            >
              {isFetching ? "Retrying…" : "Retry"}
            </button>
          </div>
        ) : !effectiveData ? (
          <p className="font-mono text-[11px] text-muted-foreground">
            {isLoading ? "Loading…" : "No data"}
          </p>
        ) : (
          <div className="space-y-4">
            <PageSection pages={pages} />
            <ShellsSection inspect={effectiveData} />
            <LivenessSection inspect={effectiveData} />
            <ConfigOverlaySection inspect={effectiveData} />
            <CostSection inspect={effectiveData} />
            <ActivitySection inspect={effectiveData} />
            <NoticeReplySection agentId={agentId} notice={effectiveData.notice ?? null} />
          </div>
        )}
      </div>
    </>
  );

  // Desktop: fixed right-side flex sibling. The 2026-08-23 ruling supersedes
  // the 2026-08-05 floating overlay for this breakpoint.
  if (isLarge) {
    return (
      <aside className={cn("w-80 shrink-0 border-l border-border bg-background", FLEX, FLEX_COL, MIN_H_0)}>
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
  return (
    <button
      type="button"
      onClick={toggle}
      data-inspector-toggle=""
      aria-label={open ? "Close inspector" : "Open inspector"}
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
      <span className="hidden sm:inline">Inspect</span>
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

function PageSection({ pages }: { pages: PageRow[] }) {
  return (
    <Section
      icon={<LayoutPanelTop className="size-3" />}
      title="Page"
      badge={pages.length > 0 ? "Open" : "None"}
    >
      {pages.length === 0 ? (
        <p className="font-mono text-[11px] text-muted-foreground/70">No open page</p>
      ) : (
        pages.map((p) => (
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
        ))
      )}
    </Section>
  );
}

// The agent's single open notice, rendered at the bottom of the panel as an
// interactive reply surface (mirrors the fleet "waiting on you" queue): a
// require_response notice gets a reply box + Dismiss, an FYI gets Mark read.
// Resolving invalidates the inspect query so the notice clears without waiting
// out the slow background interval. `notice == null` → the quiet "No open
// notice" line.

function NoticeReplySection({
  agentId,
  notice,
}: {
  agentId: number;
  notice: OpenNotice | null;
}) {
  const queryClient = useQueryClient();
  return (
    <Section icon={<Bell className="size-3" />} title="Notice" badge={notice ? "Open" : "None"}>
      {notice ? (
        // key by notice id: OpenNoticeDetail keeps `pending` true after a resolve
        // (the notice is going away), so when a refetch swaps in the NEXT open
        // notice the keyed remount gives it a fresh, enabled reply surface
        // instead of a permanently-disabled one.
        <OpenNoticeDetail
          key={notice.id}
          agentId={agentId}
          notice={notice}
          onResolved={() =>
            void queryClient.invalidateQueries({ queryKey: ["agent-inspect", agentId] })
          }
        />
      ) : (
        <p className="font-mono text-[11px] text-muted-foreground/70">No open notice</p>
      )}
    </Section>
  );
}

function ShellsSection({ inspect }: { inspect: AgentInspect }) {
  const { shells } = inspect;
  return (
    <Section
      icon={<Terminal className="size-3" />}
      title="Persistent shells"
      badge={String(shells.length)}
    >
      {shells.length === 0 ? (
        <p className="font-mono text-[11px] text-muted-foreground/70">None open</p>
      ) : (
        <ul className="space-y-1">
          {shells.map((s) => (
            <ShellRow key={s.id} agentId={inspect.agent_id} shell={s} />
          ))}
        </ul>
      )}
    </Section>
  );
}

// A shell row links to its full-screen monitor page (live terminal tail).
// Guards against NaN / missing ids from a partial API response — if either
// agentId or shell.id is not a valid finite integer, renders as plain text
// (no link) to avoid navigating to /shell/NaN/NaN.
function ShellRow({ agentId, shell }: { agentId: number; shell: ShellInfo }) {
  const validAgent = Number.isFinite(agentId) && agentId >= 0;
  const validShell = Number.isFinite(shell.id) && shell.id >= 0;
  const rowClass =
    "flex items-center gap-2 rounded bg-sidebar-accent/40 px-2 py-1 font-mono text-[11px]";
  const content = (
    <>
      <span className="tabular-nums text-muted-foreground">#{shell.id}</span>
      <span className="truncate text-foreground">{shell.name ?? "(unnamed)"}</span>
      <span className="ml-auto shrink-0 tabular-nums text-muted-foreground">
        {formatUptime(shell.uptime_seconds)}
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

function ConfigOverlaySection({ inspect }: { inspect: AgentInspect }) {
  const entries = Object.entries(inspect.config_overlay);
  return (
    <Section icon={<SlidersHorizontal className="size-3" />} title="Configuration overlay">
      {entries.length === 0 ? (
        <p className="font-mono text-[11px] text-muted-foreground/70">
          Defaults — no overrides
        </p>
      ) : (
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
      )}
    </Section>
  );
}

function CostSection({ inspect }: { inspect: AgentInspect }) {
  const { cost } = inspect;
  // Cost is the sum of stored usage-time price snapshots; calls without one
  // (unpriced model) contribute 0 and surface as the sub-line so the figure
  // is never silently partial.
  const unpricedSub = cost.unpriced_calls > 0 ? `${cost.unpriced_calls} unpriced` : undefined;
  return (
    <Section icon={<DollarSign className="size-3" />} title="Cost">
      <div className="grid grid-cols-2 gap-1">
        <Metric label="Cost" value={`$${cost.cost_usd.toFixed(4)}`} sub={unpricedSub} />
        <Metric label="LLM calls" value={String(cost.llm_calls)} />
        <Metric
          label="Tokens"
          value={`${formatTokens(cost.tokens_in)} / ${formatTokens(cost.tokens_out)}`}
        />
        <Metric label="Cache hit" value={`${cost.cache_hit_pct.toFixed(2)}%`} />
      </div>
    </Section>
  );
}

/**
 * Activity — LLM-stage throughput plus absolute time spent in LLM reasoning,
 * code execution, and idle/blocked states. The duration cells follow the
 * header window.
 */
function ActivitySection({ inspect }: { inspect: AgentInspect }) {
  const { activity, tps } = inspect;
  const hasLife = activity.alive_seconds > 0;
  const idleSeconds = Math.max(0, activity.alive_seconds - activity.active_seconds);
  return (
    <Section icon={<Timer className="size-3" />} title="Activity">
      <div className="grid grid-cols-2 gap-1">
        <Metric label="LLM stage" value={formatTps(tps.lm_stage_tps)} />
        <Metric
          label="LLM reasoning / output"
          value={
            hasLife
              ? formatInterval(Math.round(activity.llm_seconds))
              : "—"
          }
        />
        <Metric
          label="Code execution"
          value={
            hasLife
              ? formatInterval(Math.round(activity.exec_seconds))
              : "—"
          }
        />
        <Metric
          label="Idle"
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
function LivenessSection({ inspect }: { inspect: AgentInspect }) {
  const { liveness_state: state, heartbeat, spawned_at } = inspect;
  const offline = state === "offline";
  const next = nextHeartbeatCell(heartbeat);
  const lastPause = heartbeat.last_pause;
  return (
    <Section icon={<HeartPulse className={cn("size-3", offline && "text-destructive")} />} title="Liveness">
      <div className="grid grid-cols-2 gap-1">
        <Metric
          label="Birth"
          value={formatRelative(spawned_at)}
          sub={formatAbsolute(spawned_at)}
        />
        <Metric label="Next heartbeat" value={next.value} />
        <Metric
          label="Last pause"
          value={
            lastPause
              ? `${formatRelative(lastPause.at)} · ${formatInterval(Math.round(lastPause.duration_s))}`
              : "never paused"
          }
        />
      </div>
    </Section>
  );
}

// The "next heartbeat" cell — mirrors the backend's mutually-exclusive states:
// an active pause renders a clock time; an idle agent with a check-in already
// queued (the daemon won't send another while an inbound is pending) renders
// "pending"; an idle agent with nothing queued renders its projected next
// check-in; a running agent an em dash (never checked in on).
function nextHeartbeatCell(hb: HeartbeatInfo): { value: string } {
  if (hb.paused_until) {
    return {
      value: formatShort(hb.paused_until, { includeDate: false }),
    };
  }
  if (hb.heartbeat_pending) {
    return { value: "pending" };
  }
  if (hb.next_at) {
    return { value: formatRelative(hb.next_at) };
  }
  return { value: "—" };
}

function Metric({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className={cn("gap-0.5 rounded bg-sidebar-accent/40 px-2 py-1", FLEX, FLEX_COL)}>
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
  if (n < 1) return n.toFixed(1);
  if (n < 10) return n.toFixed(1);
  if (n < 100) return n.toFixed(1);
  return `${Math.round(n)}`;
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  const remH = h % 24;
  return remH ? `${d}d ${remH}h` : `${d}d`;
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
