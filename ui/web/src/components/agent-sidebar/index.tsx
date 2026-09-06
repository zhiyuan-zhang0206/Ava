"use client";

// Agent / thread sidebar — agent_id == agent_id 1:1, each row = one agent.
//
// Desktop: persistent aside on the left, sized by the homepage's resizable
//   column frame. The body already wraps its controls at narrower widths.
// Mobile (< md): fully hidden by default; the header hamburger opens a
//   full-screen overlay. Tapping a row to select an agent auto-closes
//   it back to the timeline.
//
// Content is a unified spawn tree (no spawner group header) — top level
// flattens all non-sub-agents (triggered by user / claude-code / any
// external spawner), sub-agents indent under their spawner. The roster
// always carries the terminated rows too (use-agents merges both scopes) —
// they are the lineage joints the tree walker needs, and they keep their
// true lineage position. Terminated agents are hidden from RENDERING by
// default: the toggle above the tree reveals them, and while hidden
// buildAgentTree flattens them out and re-parents their live children
// under the nearest visible ancestor (user ruling 2026-08-28 -> 09-02).
//
// Two view modes (a DB-backed user setting, display.sidebar_view_mode):
//   tree — spawn lineage tree (default)
//   flat — sortable flat list (by ID, last active, or status)
//
// Redesign (2026-07): view/terminated/sort controls consolidated into a single
// toolbar row. #723: search moved OUT of the sidebar body into a floating
// overlay (header / collapsed-rail search button), so it stays reachable even
// when the sidebar is collapsed; entering the app resets to an expanded
// sidebar + inspector (user ruling).
//
// Quiet by default (RCS): collapsed = a completely blank rail (no mini list,
// no badges, no counts — only the expand affordance); expanded shows static
// presence (agent ID / label roster) in a stable ID order. Dynamic signals are
// opt-in: status colors behind display.show_agent_status (quick toggle in the

import { useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, Search, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useState } from "react";

import { api } from "@/lib/api";
import { useBreakpoint } from "@/lib/breakpoint";
import { errMsg } from "@/lib/errors";
import { useSidebarCollapsed, useSidebarViewMode } from "@/lib/sidebar";
import { useStore } from "@/lib/store";
import { useUserSettings } from "@/lib/use-user-settings";

import { SidebarBody } from "./body";
import { SidebarFooter } from "./footer";
import { CollapsedSidebar, SidebarHeader } from "./header";
import { SearchOverlay } from "./search-overlay";
import type { DesktopProps, MobileProps, Props } from "./types";
import { FLEX, FLEX_COL } from "@/lib/layout";
import { cn } from "@/lib/utils";

export function AgentSidebar(props: Props) {
  // R4 layer 4: one responsive mechanism — breakpoint + conditional render.
  // The desktop rail and the mobile drawer are mutually exclusive surfaces;
  // only the active one mounts (the old always-mounted pair hid the inactive
  // one with CSS media queries). Switch point preserved from the old CSS
  // `md` rule: the rail shows at >= 768px, the drawer below it.
  const { isNarrow } = useBreakpoint();
  const { collapsed, setCollapsed } = useSidebarCollapsed();
  const { viewMode, setViewMode } = useSidebarViewMode();
  // Show-terminated is a DB-backed user setting (display.show_terminated), the
  // single source of truth shared with the Display settings page — toggling
  // either place updates the same ["user-settings"] cache, so they stay in
  // lockstep across the sidebar and settings. Default false
  // (USER_SETTING_DEFAULTS); opaque non-boolean DB values remain opt-out.
  // It is a RENDER-only switch: the terminated roster is fetched and merged
  // into `agents` unconditionally (its rows anchor spawn-lineage walks),
  // so this flag never gates a fetch.
  const { settings: userSettings, setSetting } = useUserSettings();
  const showTerminated = userSettings["display.show_terminated"] === true;
  const setShowTerminated = (v: boolean) => setSetting("display.show_terminated", v);
  const queryClient = useQueryClient();

  // The one-time migration of legacy localStorage preferences (including
  // ava.sidebar.showTerminated) into user_settings lives in one place:
  // lib/settings-migration.ts, mounted by <SettingsMigration/> in providers.

  // -- Read UI state from the store --
  const activeId = useStore((s) => s.activeId);
  const setActiveId = useStore((s) => s.setActiveId);
  const mobileSidebarOpen = useStore((s) => s.mobileSidebarOpen);
  const setMobileSidebarOpen = useStore((s) => s.setMobileSidebarOpen);
  const searchQuery = useStore((s) => s.searchQuery);
  const setSearchQuery = useStore((s) => s.setSearchQuery);
  const showToast = useStore((s) => s.showToast);
  const [searchOpen, setSearchOpen] = useState(false);
  const openSearch = useCallback(() => setSearchOpen(true), []);
  const closeSearch = useCallback(() => setSearchOpen(false), []);

  const { agents, pendingActions, pendingSpawnCount } = props;

  // Mobile: auto-close the drawer after selecting an agent.
  const handleSelect = useCallback(
    (id: number) => {
      setActiveId(id);
      setMobileSidebarOpen(false);
    },
    [setActiveId, setMobileSidebarOpen],
  );

  const handleRename = (id: number, label: string) => {
    api
      .patchAgentLabel(id, label)
      .then(() => queryClient.invalidateQueries({ queryKey: ["agents"] }))
      .catch((err: unknown) => {
        // Rename is a user-initiated action — a silent console.error left the
        // user thinking the new label stuck (Task #1051).
        showToast(`Failed to rename agent: ${errMsg(err)}`);
      });
  };

  return (
    <>
      {!isNarrow ? (
      <DesktopSidebar
        agents={agents}
        activeId={activeId}
        onSelect={handleSelect}
        onSpawn={props.onSpawn}
        onTerminate={props.onTerminate}
        onRestart={props.onRestart}
        onResurrect={props.onResurrect}
        onFork={props.onFork}
        onCompact={props.onCompact}
        pendingActions={pendingActions}
        pendingSpawnCount={pendingSpawnCount}
        isLoading={props.isLoading}
        onRename={handleRename}
        showTerminated={showTerminated}
        onToggleTerminated={() => setShowTerminated(!showTerminated)}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed(!collapsed)}
        onSearchOpen={openSearch}
        setCollapsed={setCollapsed}
        viewMode={viewMode}
        onToggleViewMode={() => setViewMode(viewMode === "tree" ? "flat" : "tree")}
      />
      ) : (
      <MobileSidebar
        agents={agents}
        activeId={activeId}
        onSelect={handleSelect}
        onSpawn={props.onSpawn}
        onTerminate={props.onTerminate}
        onRestart={props.onRestart}
        onResurrect={props.onResurrect}
        onFork={props.onFork}
        onCompact={props.onCompact}
        pendingActions={pendingActions}
        pendingSpawnCount={pendingSpawnCount}
        isLoading={props.isLoading}
        onRename={handleRename}
        showTerminated={showTerminated}
        onToggleTerminated={() => setShowTerminated(!showTerminated)}
        viewMode={viewMode}
        onToggleViewMode={() => setViewMode(viewMode === "tree" ? "flat" : "tree")}
        mobileOpen={mobileSidebarOpen}
        onMobileClose={() => setMobileSidebarOpen(false)}
        onSearchOpen={openSearch}
      />
      )}
      {/* Floating search overlay — search lives here, not inline in the
          sidebar (#723); the query filters the sidebar list in sync. The
          overlay mirrors the visible roster: terminated rows are always in
          `agents` (lineage joints), but search only surfaces them when the
          show-terminated toggle is on. */}
      <SearchOverlay
        open={searchOpen}
        onClose={closeSearch}
        searchQuery={searchQuery}
        onSearchQueryChange={setSearchQuery}
        agents={showTerminated ? agents : agents.filter((a) => a.status !== "terminated")}
        onSelect={handleSelect}
      />
    </>
  );
}

function DesktopSidebar(props: DesktopProps) {
  const t = useTranslations("sidebar");
  const { collapsed, setCollapsed, onSearchOpen } = props;

  if (collapsed) {
    return <CollapsedSidebar {...props} />;
  }

  return (
    <aside
      // FLEX + FLEX_COL: this aside is the I6-class vertical chain's sidebar
      // member — display:flex (R4-PR3 regression, Task #1053: `hidden md:flex`
      // was removed and only flex-col survived, so the aside fell back to
      // block layout, the agent-list ScrollArea's flex-1/min-h-0 stopped
      // bounding, and the WHOLE aside — header, spawn/toolbar bars and the
      // fixed footer — scrolled together). overflow-x-hidden is a hard
      // backstop: SidebarBody's own controls (spawn selects, sort buttons)
      // truncate to fit the available panel width, but this guarantees no
      // horizontal scrollbar can ever appear even if a control slips past that. h-full +
      // w-full let react-resizable-panels own the outer column dimensions.
      className={cn("relative h-full w-full border-r border-border bg-sidebar overflow-x-hidden", FLEX, FLEX_COL)}
    >
      <SidebarHeader
        trailing={
          <>
            {/* Search in the header (not inline in the body) — opens the
                floating overlay, #723. */}
            <button
              onClick={onSearchOpen}
              className="p-1 rounded hover:bg-sidebar-accent shrink-0"
              aria-label={t("searchAgents")}
            >
              <Search className="size-4" />
            </button>
            <button
              onClick={() => setCollapsed(true)}
              className="p-1 rounded hover:bg-sidebar-accent shrink-0"
              aria-label={t("collapseSidebar")}
            >
              <ChevronLeft className="size-4" />
            </button>
          </>
        }
      />
      <SidebarBody {...props} wide />
      {/* Fixed bottom strip (user ruling 2026-08-05): Statistics popover +
          the four nav shortcuts (Memory Graph / Fleet / Insights / Control)
          live here — the spot an app's avatar row would occupy. */}
      <SidebarFooter />
    </aside>
  );
}


function MobileSidebar(props: MobileProps) {
  const t = useTranslations("sidebar");
  const { mobileOpen, onMobileClose, onSearchOpen } = props;
  if (!mobileOpen) return null;
  return (
    <div className={cn("fixed inset-0 z-50", FLEX)}>
      <div
        className="absolute inset-0 bg-black/40"
        onClick={onMobileClose}
        aria-hidden="true"
      />
      <aside className={cn("relative w-full bg-sidebar", FLEX, FLEX_COL)}>
        <SidebarHeader
          trailing={
            <>
              <button
                onClick={onSearchOpen}
                className="p-1 rounded hover:bg-sidebar-accent shrink-0"
                aria-label={t("searchAgents")}
              >
                <Search className="size-4" />
              </button>
              <button
                onClick={onMobileClose}
                className="p-1 rounded hover:bg-sidebar-accent shrink-0"
                aria-label={t("closeSidebar")}
              >
                <X className="size-5" />
              </button>
            </>
          }
        />
        <SidebarBody {...props} wide />
        <SidebarFooter />
      </aside>
    </div>
  );
}


// ── Collapsed sidebar: a completely blank rail ──
//
// Collapsed means "I looked away — tell me nothing": no mini agent list, no
// status dots, no spinners, no badges. The only affordance is expanding —
// static presence (and any opted-in signals) live behind that deliberate pull.
