// Fleet view — full-screen supervision surface. Read-only over the agent and
// task graphs; the only writes are InboxQueue's notice reply/dismiss.
//
// The thesis: a human should be able to judge the work of 10-20 agents WITHOUT
// opening any conversation. The agents surface is the force-directed
// relationship Graph: spawn/fork/resurrect lineage as the structural skeleton,
// plus decaying agent-to-agent message traffic. Each node shows its current
// self-reported activity and links straight to its conversation page for a
// drill-down.
//
// Responsive: on desktop (>= lg / 1024px) the graph and the right-side Inbox sit
// side-by-side in a resizable split. On mobile (< lg) the surfaces — Agents
// (graph), Tasks (task graph), Inbox (the unified notice queue) — become
// full-screen tabs, each filling the viewport, with a tab bar at the bottom to
// switch between them.

"use client";

import { MessageSquare, PanelRightOpen } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { memo, useEffect, useMemo, useRef, useState } from "react";

import { GraphView } from "@/components/fleet/graph-view";
import { InboxQueue } from "@/components/fleet/inbox-queue";
import { LeftGraphPanel } from "@/components/fleet/left-graph-panel";
import { readFleetRouteIds } from "@/components/fleet/fleet-route";
import { TaskGraph } from "@/components/fleet/task-graph";
import { PluginNavIcons } from "@/components/plugin-nav";
import {
  ResizablePanel,
  ResizablePanelGroup,
  ResizableHandle,
} from "@/components/ui/resizable";
import type { AgentRow } from "@/lib/types";
import { useFleetAgents } from "@/lib/use-fleet-agents";
import { useBreakpoint } from "@/lib/breakpoint";
import { useUserSettings } from "@/lib/use-user-settings";
import { cn } from "@/lib/utils";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout";

// Which mobile tab is on screen is an EPHEMERAL, per-device selection ("which
// surface am I looking at right now"), not a durable preference — so it stays
// in localStorage rather than syncing to the DB across frontends. Written
// post-mount only (SSR renders the default; reading localStorage during render
// would mismatch hydration). The Inbox deep link is the exception: it writes
// the tab because it is the user's current surface choice.
const LS_MOBILE_TAB = "ava.fleet.mobileTab";
// The desktop left-panel view (Graph vs Task Graph) and the collapsed-queue
// choice ARE durable preferences — DB-backed (display.fleet_left_view /
// display.fleet_queue_collapsed) so they follow the user across frontends.

type MobileTab = "agents" | "tasks" | "inbox";
export function FleetView() {
  const agents = useFleetAgents();
  const { isLarge } = useBreakpoint();
  // Cross-component selection: selecting an item in the right-side
  // Decisions/Reviews panel highlights the corresponding node in the Graph, and
  // vice versa.
  const [selectedAgentId, setSelectedAgentId] = useState<number | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<number | null>(null);
  const [anchorAgentId, setAnchorAgentId] = useState<number | null>(null);
  // Route jumps from an inspector widget button (task #2909).
  const [focusNoticeId, setFocusNoticeId] = useState<number | null>(null);
  const [routeTaskId, setRouteTaskId] = useState<number | null>(null);
  // Mobile tab selection (Agents | Decisions | Reviews).
  const [mobileTab, setMobileTab] = useState<MobileTab>("agents");

  // Restore persisted state once after mount (avoids SSR hydration mismatch).
  const hydrated = useRef(false);
  useEffect(() => {
    try {
      const mt = localStorage.getItem(LS_MOBILE_TAB);
      if (mt === "agents" || mt === "tasks" || mt === "inbox") {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR-safe localStorage hydration: must run once after mount to avoid a hydration mismatch
        setMobileTab(mt);
      }
    } catch {
      // ignore malformed / unavailable storage — fall back to defaults
    }
    hydrated.current = true;
  }, []);
  useEffect(() => {
    const search = new URLSearchParams(window.location.search);
    const route = readFleetRouteIds(search);
    if (route.agentId != null) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- Route state is hydrated after mount to avoid an SSR mismatch.
      setAnchorAgentId(route.agentId);
    }
    if (route.noticeId != null) {
      setFocusNoticeId(route.noticeId);
      setMobileTab("inbox");
    }
    if (route.taskId != null) {
      setSelectedTaskId(route.taskId);
      setRouteTaskId(route.taskId);
      setMobileTab("tasks");
    } else if (window.location.hash === "#inbox" || search.get("tab") === "inbox") {
      setMobileTab("inbox");
    }
  }, []);
  useEffect(() => {
    if (hydrated.current) localStorage.setItem(LS_MOBILE_TAB, mobileTab);
  }, [mobileTab]);

  const terminatedCount = useMemo(
    () => agents.filter((a) => a.status === "terminated").length,
    [agents],
  );
  const aliveCount = agents.length - terminatedCount;

  // Inbox tab badge: total open items across the fleet, rolled up from the agent
  // snapshot — decisions awaiting a response plus unread FYI notices.
  const inboxCount = useMemo(
    () => agents.reduce((n, a) => n + a.notices_awaiting_response.length + a.unread_notice_count, 0),
    [agents],
  );

  return (
    // Task #1066: the root must FILL main's width (flex-1), not shrink to
    // its content's max-content — main is a row-flex landmark, so without
    // flex-1 a short Inbox selection collapses the whole surface to the
    // content width (observed as the UI shrinking to the left ~60%).
    <div className={cn("h-dvh bg-background text-foreground font-mono", FLEX, FLEX_COL, FLEX_1, MIN_W_0)}>
      {isLarge ? (
        <DesktopLayout
          aliveCount={aliveCount}
          agents={agents}
          selectedAgentId={selectedAgentId}
          setSelectedAgentId={setSelectedAgentId}
          selectedTaskId={selectedTaskId}
          setSelectedTaskId={setSelectedTaskId}
          anchorAgentId={anchorAgentId}
          focusNoticeId={focusNoticeId}
          routeTasksView={routeTaskId != null}
        />
      ) : (
        <MobileLayout
          aliveCount={aliveCount}
          agents={agents}
          selectedAgentId={selectedAgentId}
          setSelectedAgentId={setSelectedAgentId}
          selectedTaskId={selectedTaskId}
          setSelectedTaskId={setSelectedTaskId}
          mobileTab={mobileTab}
          setMobileTab={setMobileTab}
          inboxCount={inboxCount}
          anchorAgentId={anchorAgentId}
          focusNoticeId={focusNoticeId}
        />
      )}
    </div>
  );
}

// ── Desktop layout (>= lg): side-by-side resizable panels ──

const DesktopLayout = memo(function DesktopLayout({
  aliveCount,
  agents,
  selectedAgentId,
  setSelectedAgentId,
  selectedTaskId,
  setSelectedTaskId,
  anchorAgentId,
  focusNoticeId,
  routeTasksView,
}: {
  aliveCount: number;
  agents: AgentRow[];
  selectedAgentId: number | null;
  setSelectedAgentId: (id: number | null) => void;
  selectedTaskId: number | null;
  setSelectedTaskId: (id: number | null) => void;
  anchorAgentId: number | null;
  focusNoticeId: number | null;
  routeTasksView: boolean;
}) {
  const t = useTranslations("fleet");
  // Queue panel collapse (RCS): collapsed leaves only a STATIC handle — no
  // badge, no count, nothing that pulses in the periphery. Expanding is the
  // user's pull. DB-backed (display.fleet_queue_collapsed) so the choice follows
  // the user across frontends.
  const { settings, setSetting } = useUserSettings();
  const queueCollapsed = settings["display.fleet_queue_collapsed"] === true;
  const setQueueCollapsed = (v: boolean) => setSetting("display.fleet_queue_collapsed", v);

  return (
    <>
      <header className={cn("shrink-0 items-center gap-3 border-b border-border px-6 py-3", FLEX)}>
        <h1 className="text-sm font-semibold">{t("title")}</h1>
        <span className="text-xs text-muted-foreground tabular-nums">
          {t("activeTotal", { active: aliveCount, total: agents.length })}
        </span>
        {/* Plugin-contributed toolbar entries (contributions.ui.nav, location
            "fleet-toolbar"); renders nothing when none are declared. */}
        <div className="ml-auto">
          <PluginNavIcons location="fleet-toolbar" />
        </div>
        <Link
          href="/"
          className="p-1 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
          aria-label={t("backToConversation")}
        >
          <MessageSquare className="size-5" aria-hidden />
        </Link>
      </header>

      {queueCollapsed ? (
        <div className={cn(FLEX_1, MIN_H_0, FLEX)}>
          <div className={cn(FLEX_1, MIN_W_0, MIN_H_0)}>
            <LeftGraphPanel
              selectedAgentId={selectedAgentId}
              setSelectedAgentId={setSelectedAgentId}
              selectedTaskId={selectedTaskId}
              setSelectedTaskId={setSelectedTaskId}
              routeTasksView={routeTasksView}
            />
          </div>
          {/* Static expand handle — deliberately free of any dynamic signal. */}
          <button
            type="button"
            onClick={() => setQueueCollapsed(false)}
            aria-label={t("expandQueue")}
            className={cn("shrink-0 w-7 items-center gap-2 border-l border-border pt-3 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground", FLEX, FLEX_COL)}
          >
            <PanelRightOpen className="size-4" aria-hidden />
            <span className="text-[10px] font-medium tracking-wide [writing-mode:vertical-rl]">
              {t("queue")}
            </span>
          </button>
        </div>
      ) : (
        // The split ratio is EXEMPT from the localStorage→DB migration: it is a
        // per-viewport layout value persisted by react-resizable-panels through
        // its own synchronous Storage interface (autoSaveId), which does not
        // bridge to the async user_settings API. Kept per-device by design.
        <ResizablePanelGroup
          direction="horizontal"
          autoSaveId="ava.fleet.split"
          className={cn(FLEX_1, MIN_H_0)}
        >
          <ResizablePanel defaultSize={52} minSize={25}>
            <LeftGraphPanel
              selectedAgentId={selectedAgentId}
              setSelectedAgentId={setSelectedAgentId}
              selectedTaskId={selectedTaskId}
              setSelectedTaskId={setSelectedTaskId}
              routeTasksView={routeTasksView}
            />
          </ResizablePanel>
          <ResizableHandle />
          <ResizablePanel defaultSize={48} minSize={30}>
            <InboxQueue
              agents={agents}
              className="h-full"
              selectedAgentId={selectedAgentId}
              onSelectAgent={setSelectedAgentId}
              onCollapse={() => setQueueCollapsed(true)}
              anchorAgentId={anchorAgentId}
              focusNoticeId={focusNoticeId}
            />
          </ResizablePanel>
        </ResizablePanelGroup>
      )}
    </>
  );
});

// ── Mobile layout (< lg): full-screen tabs ──

const MobileLayout = memo(function MobileLayout({
  aliveCount,
  agents,
  selectedAgentId,
  setSelectedAgentId,
  selectedTaskId,
  setSelectedTaskId,
  mobileTab,
  setMobileTab,
  inboxCount,
  anchorAgentId,
  focusNoticeId,
}: {
  aliveCount: number;
  agents: AgentRow[];
  selectedAgentId: number | null;
  setSelectedAgentId: (id: number | null) => void;
  selectedTaskId: number | null;
  setSelectedTaskId: (id: number | null) => void;
  mobileTab: MobileTab;
  setMobileTab: (t: MobileTab) => void;
  inboxCount: number;
  anchorAgentId: number | null;
  focusNoticeId: number | null;
}) {
  const t = useTranslations("fleet");
  return (
    <>
      {/* Mobile header (compact) */}
      <header className={cn("shrink-0 items-center gap-2 border-b border-border px-4 py-2", FLEX)}>
        <h1 className="text-sm font-semibold">{t("title")}</h1>
        <span className="text-[11px] text-muted-foreground tabular-nums">
          {t("activeTotal", { active: aliveCount, total: agents.length })}
        </span>
        <Link
          href="/"
          className="ml-auto p-1 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
          aria-label={t("backToConversation")}
        >
          <MessageSquare className="size-4" aria-hidden />
        </Link>
      </header>

      {/* Tab content — fills all available space above the tab bar.
           One surface mounted at a time (R4 layer 4: conditional render). */}
      <div className={cn("relative", FLEX_1, MIN_H_0, MIN_W_0)}>
        {mobileTab === "agents" ? (
          <div className="absolute inset-0">
            <GraphView
              selectedAgentId={selectedAgentId}
              onSelectAgent={setSelectedAgentId}
            />
          </div>
        ) : null}
        {mobileTab === "tasks" ? (
          <div className="absolute inset-0">
            <TaskGraph
              selectedTaskId={selectedTaskId}
              onSelectTask={setSelectedTaskId}
              selectedAgentId={selectedAgentId}
              onSelectAgent={setSelectedAgentId}
            />
          </div>
        ) : null}
        {mobileTab === "inbox" ? (
          <InboxQueue
            agents={agents}
            className="h-full"
            onSelectAgent={setSelectedAgentId}
            anchorAgentId={anchorAgentId}
            focusNoticeId={focusNoticeId}
            compact
          />
        ) : null}
      </div>

      {/* Tab bar — fixed at the bottom */}
      <div
        className={cn("shrink-0 items-center border-t border-border bg-background", FLEX)}
        role="tablist"
        aria-label={t("sections")}
      >
        <MobileTabButton
          label={t("agents")}
          count={aliveCount}
          active={mobileTab === "agents"}
          onClick={() => setMobileTab("agents")}
        />
        <MobileTabButton
          label={t("tasks")}
          count={0}
          active={mobileTab === "tasks"}
          onClick={() => setMobileTab("tasks")}
        />
        <MobileTabButton
          label={t("inbox")}
          count={inboxCount}
          active={mobileTab === "inbox"}
          onClick={() => setMobileTab("inbox")}
        />
      </div>
    </>
  );
});

// ── Shared sub-components ──

// Mobile tab button — full-width, equal share; same visual vocabulary as TabButton.
function MobileTabButton({
  label,
  count,
  active,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "items-center justify-center gap-1.5 py-3 text-xs font-medium transition-colors",
        active
          ? "bg-sidebar-accent text-foreground border-t-2 border-primary -mt-px"
          : "text-muted-foreground hover:text-foreground border-t-2 border-transparent",
          FLEX_1, FLEX
      )}
    >
      {label}
      {count > 0 && (
        <span className="rounded-full bg-muted px-1.5 text-[10px] tabular-nums text-foreground">
          {count}
        </span>
      )}
    </button>
  );
}
