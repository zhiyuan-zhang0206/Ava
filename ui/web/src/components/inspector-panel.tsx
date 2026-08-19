"use client";

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bell,
  DollarSign,
  ExternalLink,
  Gauge,
  HeartPulse,
  LayoutPanelTop,
  PanelTopClose,
  RefreshCw,
  SlidersHorizontal,
  Terminal,
  Timer,
  X,
} from "lucide-react";
import Link from "next/link";
import { useCallback, type ReactNode, useEffect, useRef, useState } from "react";

import { OpenNoticeDetail } from "@/components/open-notice-detail";
import { api } from "@/lib/api";
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
import { BAR_HEIGHT_CLASS, FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout";

// Window options for the cost + activity sections. `null` = cumulative since
// spawn (the default); the hour values are a subset of the backend whitelist
// (StatsWindowHours: 1/6/24/72/168). -1 is a local sentinel for the
// since-last-compact window — it never reaches the backend as `hours`, it
// selects the `since_compact` query param instead.
const COMPACT_WINDOW = -1;
const WINDOWS: { label: string; value: number | null }[] = [
  { label: "All", value: null },
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
 * window selector re-scopes cost (default: cumulative since spawn).
 *
 * Responsive: on desktop (≥ lg) it's a side panel; on mobile (< lg) it
 * becomes a full-screen overlay with a backdrop — same pattern as the
 * mobile sidebar drawer.
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
  const [hours, setHours] = useState<number | null>(null);

  // Context-bar style floating panel (user ruling 2026-08-05 21:50): the
  // panel pops up from the composer's top-right corner (anchored to the
  // Inspect toggle) instead of a full-height right-side drawer — no backdrop.
  // Escape, the header X, and a click anywhere outside the panel all close it.
  const panelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") toggle();
    };
    const onDown = (e: MouseEvent) => {
      // Never treat the Inspect toggle itself as "outside" — its own click
      // already toggles. Without this exclusion a second click on the toggle
      // fires mousedown(close) + click(open) and the panel never closes
      // (user ruling 2026-08-06: second click must collapse).
      const t = e.target as Element | null;
      if (t?.closest("[data-inspector-toggle]")) return;
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        toggle();
      }
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
    };
  }, [open, toggle]);

  const { data, error, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["agent-inspect", agentId, hours],
    queryFn: () =>
      api.getAgentInspect(
        agentId,
        hours === COMPACT_WINDOW ? null : hours,
        hours === COMPACT_WINDOW,
      ),
    // Slow background drift only — the endpoint is expensive (~25 Loki
    // aggregations + a shell-probe RPC per call), so the interval is a floor
    // for "numbers don't rot while the panel sits open", not the freshness
    // mechanism. Freshness comes from refetchOnMount on open, the header's
    // manual refresh, and SSE-driven invalidation (notices below).
    refetchInterval: 60_000,
    // Fetch once on open, not just cold. The global 5min staleTime otherwise
    // treats cached inspect data (from the toggle's intent prefetch or a
    // previous open) as fresh, so opening the panel would show stale numbers;
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

  // Open pages: SSE-driven cache (page_opened/page_closed fold in live), not a
  // poll — see useAgentPages.
  const pages = useAgentPages(agentId);
  const queryClient = useQueryClient();

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
  // query key resets (agentId / hours change). During the fetch the previous
  // agent's data stays on screen; the new data replaces it the moment it lands.
  const [lastData, setLastData] = useState<AgentInspect>();
  // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: cache last successful data to avoid blanking
  useEffect(() => { if (data) setLastData(data); }, [data]);
  // Cross-agent guard (Task #1051): on agent switch, drop the previous
  // agent's snapshot immediately — while the new query is in flight, `data`
  // is undefined for a COLD key and effectiveData would otherwise fall back
  // to agent A's snapshot and render it (shells/cost/notice — and the
  // NoticeReplySection reply/read writes) under agent B's name.
  // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: stale snapshot must not survive an agent switch
  useEffect(() => { setLastData(undefined); }, [agentId]);
  const effectiveData = data ?? lastData;

  // Renders nothing while closed — the panel floats above the layout (fixed),
  // so it leaves the flex column entirely instead of reserving width. All
  // hooks run regardless (rules-of-hooks); the toggle's intent prefetch keeps
  // the cache warm for the next open.
  if (!open) return null;

  // Context-bar style floating panel (user ruling 2026-08-05 21:50):
  // anchored to the composer's top-right corner, pops upward — no backdrop,
  // no full-height drawer. Escape / header X / outside click close it.
  return (
    <aside
      ref={panelRef}
      className={cn("absolute bottom-full right-0 z-50 mb-2 w-[22rem] max-w-[calc(100vw-2rem)] rounded-lg border border-border bg-background shadow-xl", FLEX, FLEX_COL, OVERFLOW_HIDDEN)}
    >
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

        <div className={cn("max-h-[65vh] overflow-y-auto px-4 py-3 text-xs", MIN_H_0)}>
          {error && !effectiveData ? (
            <p className="font-mono text-[11px] text-destructive">
              {error instanceof Error ? error.message : "Failed to load"}
            </p>
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
              <TpsSection inspect={effectiveData} />
              <NoticeReplySection agentId={agentId} notice={effectiveData.notice ?? null} />
            </div>
          )}
        </div>
    </aside>
  );
}

/** Header toggle button — opens/closes the inspector. Rendered in the
 *  composer's top-right corner (user ruling 2026-08-05).
 *
 *  Prefetch-on-intent: hovering/focusing the toggle prefetches the default
 *  inspect window so the panel opens populated — this replaces the old
 *  selection-time preload, which fired the expensive /inspect call for every
 *  agent clicked in the sidebar whether or not the panel ever opened. Intent
 *  (pointer on the toggle) is the earliest signal that the data will actually
 *  be looked at. */
export function InspectorToggle({ agentId = null }: { agentId?: number | null }) {
  const { open, toggle } = useInspectorOpen();
  const queryClient = useQueryClient();
  const prefetch = useCallback(() => {
    if (agentId == null) return;
    void queryClient.prefetchQuery({
      queryKey: ["agent-inspect", agentId, null],
      queryFn: () => api.getAgentInspect(agentId, null, false),
      // Don't re-fire on every pointer wiggle; the panel's own
      // refetchOnMount:"always" pulls a fresh snapshot when it opens.
      staleTime: 10_000,
    });
  }, [agentId, queryClient]);
  return (
    <button
      type="button"
      onClick={toggle}
      onPointerEnter={prefetch}
      onFocus={prefetch}
      data-inspector-toggle=""
      aria-label={open ? "Close inspector" : "Open inspector"}
      className={cn(
        "inline-flex items-center gap-1.5 rounded border px-1.5 py-0.5 font-mono text-[11px] transition-colors select-none",
        open
          ? "border-border bg-accent text-accent-foreground"
          : "border-transparent text-muted-foreground/50 hover:border-border hover:text-muted-foreground",
      )}
    >
      {/* Arrow points up toward the top bar regardless of state (user ruling
          8/6, re-affirmed 8/8 #1065): PanelTopOpen's chevron points DOWN, so
          PanelTopClose is the up-arrow. Selected state = brightness, not
          direction. */}
      <PanelTopClose className="size-3.5" />
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
 * Active rate — what share of the agent's alive time it spent actively working
 * (in the graph: llm + exec + hooks) versus idle-waiting for input. The
 * complement is "blocked on input" — the share of life gated on a human/peer.
 * A project-lead agent that decides every minute reads near 100%; one woken
 * once a week reads near 0%. The headline is the percentage; the two cells give
 * the absolute active and idle/blocked durations. Follows the header window.
 */
function ActivitySection({ inspect }: { inspect: AgentInspect }) {
  const { activity } = inspect;
  const hasLife = activity.alive_seconds > 0;
  const pct = Math.round(activity.active_rate * 100);
  const idleSeconds = Math.max(0, activity.alive_seconds - activity.active_seconds);
  return (
    <Section icon={<Timer className="size-3" />} title="Active rate">
      <div className="grid grid-cols-2 gap-1">
        <Metric label="Active rate" value={hasLife ? `${pct}%` : "—"} />
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

function TpsSection({ inspect }: { inspect: AgentInspect }) {
  const { tps } = inspect;
  return (
    <Section icon={<Gauge className="size-3" />} title="TPS">
      <Metric
        label="LLM stage"
        value={formatTps(tps.lm_stage_tps)}
      />
    </Section>
  );
}

/**
 * Liveness — one merged section (Task #1195, user ruling 2026-08-12): the
 * gateway-owned derived liveness state (Task #1174) plus the agent's birth
 * and idle-heartbeat state. 2×2 grid: row 1 = liveness state + birth; row 2 =
 * the heartbeat cells (next nudge / last pause). Heartbeat's icon carries the
 * "Liveness" title; the "every N" badge and the old "Last judged" cell are
 * dropped (the interval badge reads as "every 5 minutes").
 * 'online' = the machine is reachable AND (for running/idling) the process
 * lease is alive; 'offline' = the machine dropped or the process died (the
 * agent list shows a grey dot); 'unknown' = the gateway has not judged this
 * row yet.
 */
function LivenessSection({ inspect }: { inspect: AgentInspect }) {
  const { liveness_state: state, heartbeat, spawned_at } = inspect;
  const offline = state === "offline";
  const next = nextHeartbeatCell(heartbeat);
  const lastPause = heartbeat.last_pause;
  return (
    <Section icon={<HeartPulse className={cn("size-3", offline && "text-destructive")} />} title="Liveness">
      <div className="grid grid-cols-2 gap-1">
        <Metric label="State" value={state} />
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
