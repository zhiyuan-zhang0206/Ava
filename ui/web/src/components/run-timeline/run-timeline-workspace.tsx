"use client";

// The page body: the timeline and its context on the left, the details of the
// selection on the right. Wide viewports split the two with a draggable
// handle (the repo's react-resizable-panels wrapper) whose side panel is
// bounded in pixels and whose ratio is remembered per device; narrow viewports
// stack them.

import { useTranslations } from "next-intl";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { useDefaultLayout } from "react-resizable-panels";

import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { useBreakpoint } from "@/lib/layout/breakpoint";
import { FLEX_1, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { panelLayoutStorage } from "@/lib/layout/panel-layout-storage";
import { cn } from "@/lib/format/utils";

export const RUN_TIMELINE_SPLIT_LAYOUT_ID = "ava.run-timeline.split";
const PANEL_MAIN = "panel-run-timeline-main";
const PANEL_SIDE = "panel-run-timeline-side";
const RUN_TIMELINE_SPLIT_STORAGE = panelLayoutStorage();

/** The side panel's width bounds, in pixels. */
export const SIDE_PANEL_DEFAULT = "440px";
export const SIDE_PANEL_MIN = "300px";
export const SIDE_PANEL_MAX = "760px";
const MAIN_PANEL_MIN = "420px";

export function RunTimelineWorkspace({ main, side }: { main: ReactNode; side: ReactNode }) {
  const t = useTranslations("runTimeline");
  const { isWide } = useBreakpoint();
  // The split mounts only once the breakpoint is known, as the memory graph's does:
  // the pre-mount default is the narrow frame, and a group registered under it would
  // re-register the moment the real breakpoint resolves.
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR-safe: runs once after mount so the first painted frame has the real breakpoint
    setMounted(true);
  }, []);
  const split = useDefaultLayout({
    id: RUN_TIMELINE_SPLIT_LAYOUT_ID,
    storage: RUN_TIMELINE_SPLIT_STORAGE,
    onlySaveAfterUserInteractions: true,
  });

  const sideRegion = (
    <aside
      data-testid="run-timeline-reader"
      aria-label={t("detailRegion")}
      className={cn("h-full overflow-y-auto px-4 py-3.5 [overflow-wrap:anywhere]", MIN_H_0)}
    >
      {side}
    </aside>
  );

  if (!mounted || !isWide) {
    return (
      <div className={cn("overflow-y-auto", FLEX_1, MIN_H_0)} data-testid="run-timeline-workspace">
        <div className={cn("space-y-5 p-4", MIN_W_0)}>{main}</div>
        <div className="border-t border-border bg-card">{sideRegion}</div>
      </div>
    );
  }

  return (
    <ResizablePanelGroup
      orientation="horizontal"
      defaultLayout={split.defaultLayout}
      onLayoutChanged={split.onLayoutChanged}
      className={cn(FLEX_1, MIN_H_0)}
      data-testid="run-timeline-workspace"
    >
      <ResizablePanel id={PANEL_MAIN} minSize={MAIN_PANEL_MIN}>
        <div data-testid="run-timeline-main" className={cn("h-full overflow-y-auto", MIN_W_0)}>
          <div className="space-y-5 p-6">{main}</div>
        </div>
      </ResizablePanel>
      <ResizableHandle aria-label={t("resizeHandle")} />
      <ResizablePanel
        id={PANEL_SIDE}
        defaultSize={SIDE_PANEL_DEFAULT}
        minSize={SIDE_PANEL_MIN}
        maxSize={SIDE_PANEL_MAX}
        className="bg-card"
      >
        {sideRegion}
      </ResizablePanel>
    </ResizablePanelGroup>
  );
}
