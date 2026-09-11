"use client";

import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { useDefaultLayout } from "react-resizable-panels";

import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { BAR_HEIGHT_PX, FLEX, FLEX_1, MIN_H_0, MIN_W_0 } from "@/lib/layout";
import { panelLayoutStorage } from "@/lib/panel-layout-storage";
import { cn } from "@/lib/utils";

// Panel sizes are written as explicit percent strings: v4 reads a bare number
// as PIXELS and a unit-less string as percent (v3 read numbers as percent).
interface ColumnFrame {
  layoutId: string;
  expanded: readonly [sidebar: string, main: string];
  collapsedSidebarSize: number;
  expandedMinimums: readonly [sidebar: string, main: string];
}

const DESKTOP_COLUMNS: ColumnFrame = {
  layoutId: "ava.home.columns.desktop",
  expanded: ["30%", "70%"],
  collapsedSidebarSize: 3,
  expandedMinimums: ["20%", "45%"],
};

const MOBILE_COLUMNS: ColumnFrame = {
  layoutId: "ava.home.columns.mobile",
  expanded: ["40%", "60%"],
  collapsedSidebarSize: 5,
  expandedMinimums: ["30%", "40%"],
};

const INSPECTOR_LAYOUT_ID = "ava.home.inspector.desktop";
const INSPECTOR_OPEN_SIZES = ["68%", "32%"] as const;

// v4 stores a group's layout as { <panelId>: percent }, so panel ids are part
// of the persisted state (the library also writes them to id/data-testid on
// the panel node — keep them unique across the app).
const PANEL_SIDEBAR = "panel-sidebar";
const PANEL_MAIN = "panel-main";
const PANEL_TIMELINE = "panel-timeline";
const PANEL_INSPECTOR = "panel-inspector";

const COLUMNS_PANEL_IDS = [PANEL_SIDEBAR, PANEL_MAIN] as const;
const TIMELINE_PANEL_IDS = [PANEL_TIMELINE] as const;
const TIMELINE_AND_INSPECTOR_PANEL_IDS = [PANEL_TIMELINE, PANEL_INSPECTOR] as const;

// One storage object per panel set — it carries the v3 -> v4 layout bridge, so
// it must know which panels the group renders right now (see
// lib/panel-layout-storage.ts).
const COLUMNS_STORAGE = panelLayoutStorage(COLUMNS_PANEL_IDS);
const TIMELINE_STORAGE = panelLayoutStorage(TIMELINE_PANEL_IDS);
const TIMELINE_AND_INSPECTOR_STORAGE = panelLayoutStorage(TIMELINE_AND_INSPECTOR_PANEL_IDS);

// The painted divider starts below the 44px shared title bar and the 40px
// column-title row. Its 89px bottom gap ends at the composer's measured top
// edge (y=750 in the 839px acceptance viewport). The handle itself remains
// full-height; only its `after` paint segment uses these insets.
const HOME_DIVIDER_COLUMN_TITLE_ROW_HEIGHT_PX = 40;
const HOME_DIVIDER_BOTTOM_INSET_PX = 89;
const HOME_DIVIDER_LINE_CLASS =
  "after:top-[var(--home-divider-line-top)] after:bottom-[var(--home-divider-line-bottom)]";
const HOME_DIVIDER_LINE_STYLE = {
  "--home-divider-line-top": `${BAR_HEIGHT_PX + HOME_DIVIDER_COLUMN_TITLE_ROW_HEIGHT_PX}px`,
  "--home-divider-line-bottom": `${HOME_DIVIDER_BOTTOM_INSET_PX}px`,
} as CSSProperties;

interface Props {
  isNarrow: boolean;
  isLarge: boolean;
  sidebarCollapsed: boolean;
  sidebar: ReactNode;
  main: ReactNode;
  inspector: ReactNode;
}

function HomeDividerHandle() {
  return <ResizableHandle className={HOME_DIVIDER_LINE_CLASS} style={HOME_DIVIDER_LINE_STYLE} />;
}

function DesktopMain({ main, inspector }: Pick<Props, "main" | "inspector">) {
  const inspectorVisible = inspector !== null && inspector !== undefined && inspector !== false;
  const mainDefaultSize = inspectorVisible ? INSPECTOR_OPEN_SIZES[0] : "100%";

  // Persist only user-driven layout commits: a mount / shape-change /
  // constraint commit must not overwrite this shape's stored split with a
  // normalized transient frame (the job the removed v3 mount guard did).
  // The closed-inspector shape keeps its own panel set, so toggling the
  // inspector never touches the dragged two-panel split.
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({
    id: INSPECTOR_LAYOUT_ID,
    storage: inspectorVisible ? TIMELINE_AND_INSPECTOR_STORAGE : TIMELINE_STORAGE,
    onlySaveAfterUserInteractions: true,
  });

  return (
    // Keep this group horizontal for its whole lifetime. Re-orienting (or
    // re-shaping) a mounted react-resizable-panels group can normalize and
    // commit a breakpoint frame over the user's saved desktop split.
    <ResizablePanelGroup
      orientation="horizontal"
      defaultLayout={defaultLayout}
      onLayoutChanged={onLayoutChanged}
      className={cn(FLEX_1, MIN_H_0, MIN_W_0)}
    >
      <ResizablePanel
        id={PANEL_TIMELINE}
        defaultSize={mainDefaultSize}
        minSize={inspectorVisible ? "50%" : "100%"}
        className={cn(FLEX, MIN_H_0, MIN_W_0)}
      >
        {main}
      </ResizablePanel>
      {inspectorVisible ? (
        <>
          <HomeDividerHandle />
          <ResizablePanel
            id={PANEL_INSPECTOR}
            defaultSize={INSPECTOR_OPEN_SIZES[1]}
            minSize="25%"
            className={cn(FLEX, MIN_H_0, MIN_W_0)}
          >
            {inspector}
          </ResizablePanel>
        </>
      ) : null}
    </ResizablePanelGroup>
  );
}

export function HomeLayout({
  isNarrow,
  isLarge,
  sidebarCollapsed,
  sidebar,
  main,
  inspector,
}: Props) {
  // useBreakpoint intentionally starts in its SSR-safe mobile frame. Delay
  // PanelGroup registration until its effects have installed the real frame;
  // otherwise the transient frame registers a group that immediately remounts
  // onto the breakpoint's keyed group. The full-size placeholder reserves the
  // page box, so the gate itself does not move surrounding layout.
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- the first registered panel frame must use post-mount breakpoint state
    setMounted(true);
  }, []);

  const frame = isLarge ? DESKTOP_COLUMNS : MOBILE_COLUMNS;
  // Only user-driven commits persist (see DesktopMain) — the breakpoint's
  // frame swap must never rewrite the other frame's saved ratios.
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({
    id: frame.layoutId,
    storage: COLUMNS_STORAGE,
    onlySaveAfterUserInteractions: true,
  });

  if (!mounted) {
    return (
      <div
        data-testid="home-layout-placeholder"
        aria-hidden="true"
        className={cn(FLEX_1, MIN_H_0, MIN_W_0)}
      />
    );
  }

  // Phones retain the existing full-screen sidebar and inspector overlays.
  // They do not mount a split group, so their interaction model cannot be
  // constrained by a desktop panel's minimum width.
  if (isNarrow) {
    return (
      <>
        {sidebar}
        {main}
        {inspector}
      </>
    );
  }

  if (sidebarCollapsed) {
    return (
      <>
        {/* A collapsed rail is not user-resizable, so it stays outside rrp:
            a group here would register a shape the user can never adjust. */}
        <div className={cn("h-full w-full", FLEX, FLEX_1, MIN_H_0, MIN_W_0)}>
          <div
            className={cn(FLEX, MIN_H_0, MIN_W_0)}
            style={{
              flexBasis: `${frame.collapsedSidebarSize}%`,
              flexGrow: 0,
              flexShrink: 0,
            }}
          >
            {sidebar}
          </div>
          <div aria-hidden data-slot="static-divider" className="w-px shrink-0 bg-border" />
          <div className={cn(FLEX, FLEX_1, MIN_H_0, MIN_W_0)}>
            {isLarge && inspector !== null && inspector !== undefined && inspector !== false ? (
              <DesktopMain main={main} inspector={inspector} />
            ) : (
              main
            )}
          </div>
        </div>
        {isLarge ? null : inspector}
      </>
    );
  }

  return (
    <>
      {/* Desktop and compact frames have different layout ids, so breakpoint
          transitions cannot overwrite each other's saved ratios. */}
      <ResizablePanelGroup
        key={frame.layoutId}
        orientation="horizontal"
        defaultLayout={defaultLayout}
        onLayoutChanged={onLayoutChanged}
        className={cn(FLEX_1, MIN_H_0, MIN_W_0)}
      >
        <ResizablePanel
          id={PANEL_SIDEBAR}
          defaultSize={frame.expanded[0]}
          minSize={frame.expandedMinimums[0]}
          maxSize="50%"
          className={cn(FLEX, MIN_H_0, MIN_W_0)}
        >
          {sidebar}
        </ResizablePanel>
        <HomeDividerHandle />
        <ResizablePanel
          id={PANEL_MAIN}
          defaultSize={frame.expanded[1]}
          minSize={frame.expandedMinimums[1]}
          className={cn(FLEX, MIN_H_0, MIN_W_0)}
        >
          {isLarge ? <DesktopMain main={main} inspector={inspector} /> : main}
        </ResizablePanel>
      </ResizablePanelGroup>
      {/* Below lg the inspector remains the existing fixed overlay. Keeping it
          outside the md-width split prevents a hidden panel from consuming
          timeline width and avoids clipping position:fixed under a panel. */}
      {isLarge ? null : inspector}
    </>
  );
}
