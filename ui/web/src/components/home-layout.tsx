"use client";

import { useEffect, useState, type ReactNode } from "react";

import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { FLEX, FLEX_1, MIN_H_0, MIN_W_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

interface ColumnFrame {
  autoSaveId: string;
  expanded: readonly [sidebar: number, main: number];
  collapsedSidebarSize: number;
  expandedMinimums: readonly [sidebar: number, main: number];
}

const DESKTOP_COLUMNS: ColumnFrame = {
  autoSaveId: "ava.home.columns.desktop",
  expanded: [30, 70],
  collapsedSidebarSize: 3,
  expandedMinimums: [20, 45],
};

const MOBILE_COLUMNS: ColumnFrame = {
  autoSaveId: "ava.home.columns.mobile",
  expanded: [40, 60],
  collapsedSidebarSize: 5,
  expandedMinimums: [30, 40],
};

const INSPECTOR_AUTO_SAVE_ID = "ava.home.inspector.desktop";
const INSPECTOR_OPEN_SIZES = [68, 32] as const;

interface Props {
  isNarrow: boolean;
  isLarge: boolean;
  sidebarCollapsed: boolean;
  sidebar: ReactNode;
  main: ReactNode;
  inspector: ReactNode;
}

function DesktopMain({ main, inspector }: Pick<Props, "main" | "inspector">) {
  const inspectorVisible = inspector !== null && inspector !== undefined && inspector !== false;
  const mainDefaultSize = inspectorVisible ? INSPECTOR_OPEN_SIZES[0] : 100;

  return (
    // Keep this group horizontal for its whole lifetime. Changing direction
    // on a mounted react-resizable-panels group can normalize and persist a
    // breakpoint frame over the user's saved desktop split.
    <ResizablePanelGroup
      direction="horizontal"
      autoSaveId={INSPECTOR_AUTO_SAVE_ID}
      className={cn(FLEX_1, MIN_H_0, MIN_W_0)}
    >
      <ResizablePanel
        defaultSize={mainDefaultSize}
        minSize={inspectorVisible ? 50 : 100}
        className={cn(FLEX, MIN_H_0, MIN_W_0)}
      >
        {main}
      </ResizablePanel>
      {inspectorVisible ? (
        <>
          <ResizableHandle />
          <ResizablePanel
            defaultSize={INSPECTOR_OPEN_SIZES[1]}
            minSize={25}
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
  // otherwise rrp v3 normalizes that transient layout and saves it over the
  // user's dragged ratios. The full-size placeholder reserves the page box,
  // so the gate itself does not move surrounding layout.
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- the first registered panel frame must use post-mount breakpoint state
    setMounted(true);
  }, []);

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

  const frame = isLarge ? DESKTOP_COLUMNS : MOBILE_COLUMNS;

  if (sidebarCollapsed) {
    return (
      <>
        {/* A collapsed rail is not user-resizable, so keep it outside rrp.
            Mounting a constrained PanelGroup here lets rrp v3 persist the
            rail minimum over the user's expanded split during a round trip. */}
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
      {/* Desktop and compact frames have different autoSaveIds, so breakpoint
          transitions cannot overwrite each other's saved ratios. */}
      <ResizablePanelGroup
        key={frame.autoSaveId}
        direction="horizontal"
        autoSaveId={frame.autoSaveId}
        className={cn(FLEX_1, MIN_H_0, MIN_W_0)}
      >
        <ResizablePanel
          defaultSize={frame.expanded[0]}
          minSize={frame.expandedMinimums[0]}
          maxSize={50}
          className={cn(FLEX, MIN_H_0, MIN_W_0)}
        >
          {sidebar}
        </ResizablePanel>
        <ResizableHandle />
        <ResizablePanel
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
