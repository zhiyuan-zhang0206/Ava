"use client";

// The compare view body: every lane fetches the SAME explicit [from, to]
// window (alignment by construction, never by adopting whichever lane answered
// first), renders through the single-view chart component, and shares one
// sticky toolbar. Message arrows come from compare-arrows.tsx.

import { keepPreviousData, useQueries } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { createRef, useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  COMPARE_LANE_HUES,
  CompareArrows,
  type CompareArrowHover,
} from "@/components/run-timeline/compare-arrows";
import {
  bucketLabel,
  centerZoomWindow,
  needsBuckets,
  pickBucketSeconds,
  usesTimelineBuckets,
  type TimelineWindowOverride,
} from "@/components/run-timeline/request-level";
import {
  MIN_DETAIL_CANVAS_WIDTH,
  RunTimelineChart,
} from "@/components/run-timeline/run-timeline-chart";
import { tickLabel } from "@/components/run-timeline/run-timeline-details";
import { buttonVariants } from "@/components/ui/button";
import { api } from "@/lib/api";
import { BREAKPOINT_LG_PX } from "@/lib/breakpoint";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout";
import type { RunTimelineResponse } from "@/lib/types";
import { useMediaQuery } from "@/lib/use-media-query";
import { useUserSettings } from "@/lib/use-user-settings";
import { cn } from "@/lib/utils";

import {
  RUN_TIMELINE_SUMMARY_VISIBLE_SETTING,
  RUN_TIMELINE_WINDOW_HOURS_SETTING,
  RUN_TIMELINE_ZOOM_HOURS,
  chartLabels,
  dateTimeInputValue,
  initialTimelineWindow,
  runTimelineWindowHours,
  zoomPresetLabel,
} from "../_run-timeline-shared";

/** The sub-window every lane's loaded rows share — the pilot's "common window".
 *  Null when a lane has no rows or the extents do not overlap. */
export function overlapWindow(
  timelines: (RunTimelineResponse | undefined)[],
): TimelineWindowOverride | null {
  let from = -Infinity;
  let to = Infinity;
  for (const timeline of timelines) {
    const rows = timeline?.rows ?? [];
    if (rows.length === 0) return null;
    let laneFrom = Infinity;
    let laneTo = -Infinity;
    for (const row of rows) {
      laneFrom = Math.min(laneFrom, Date.parse(row.start));
      laneTo = Math.max(laneTo, Date.parse(row.end));
    }
    from = Math.max(from, laneFrom);
    to = Math.min(to, laneTo);
  }
  if (!(from < to)) return null;
  return { from: new Date(from).toISOString(), to: new Date(to).toISOString() };
}

/** Metrics of one compare lane's chart column (the lane row minus its label). */
const LANE_LABEL_COLUMN_PX = 112; // w-28
const LANE_GAP_PX = 12; // gap-3
const PANEL_COLUMN_PX = 320; // the chart's detail column
const PANEL_GRID_GAP_PX = 12; // grid gap-3
const SECTION_PADDING_PX = 12; // the chart section's p-3, each side
const SECTION_BORDER_PX = 1; // the chart section's hairline border, each side

export function laneColumnWidth(stackWidth: number): number {
  return Math.max(0, Math.floor(stackWidth) - LANE_LABEL_COLUMN_PX - LANE_GAP_PX);
}

/** The one canvas width every compare lane renders (task #3802): any lane's
 *  open detail panel narrows every lane together (side-by-side layout only),
 *  so a delivery timestamp keeps a single x across the stacked axes. The
 *  width is the chart's real container (padding and border removed, plus the
 *  detail column when open) — a canvas wider than its container would hide
 *  its edge ticks behind the overflow, and per-lane horizontal scrolling has
 *  no value once every lane opens on the same slot. Floor: the same 320 used
 *  when the chart squeezes beside a detail panel. */
export function sharedCanvasWidth(
  columnWidth: number,
  anyDetailOpen: boolean,
  wideLayout: boolean,
): number {
  const panel = anyDetailOpen && wideLayout ? PANEL_COLUMN_PX + PANEL_GRID_GAP_PX : 0;
  return Math.max(
    MIN_DETAIL_CANVAS_WIDTH,
    columnWidth - SECTION_PADDING_PX * 2 - SECTION_BORDER_PX * 2 - panel,
  );
}

export function CompareView({
  agents,
  names,
}: {
  agents: number[];
  names: Map<number, string | null>;
}) {
  const t = useTranslations("runTimeline");
  const { settings, isLoading: settingsLoading } = useUserSettings();
  const configuredWindowHours = runTimelineWindowHours(settings[RUN_TIMELINE_WINDOW_HOURS_SETTING]);
  const showSummaries = settings[RUN_TIMELINE_SUMMARY_VISIBLE_SETTING] !== false;
  const labels = useMemo(() => chartLabels(t), [t]);
  const [session, setSession] = useState<"compact" | "current">("compact");
  const [arrowsVisible, setArrowsVisible] = useState(true);
  const [hoveredArrow, setHoveredArrow] = useState<CompareArrowHover | null>(null);
  const stackRef = useRef<HTMLDivElement>(null);
  const laneRefs = useMemo(() => agents.map(() => createRef<HTMLDivElement>()), [agents]);
  const [stackWidth, setStackWidth] = useState(0);
  const [detailOpenLanes, setDetailOpenLanes] = useState<ReadonlySet<number>>(new Set());
  const wideLayout = useMediaQuery(`(min-width: ${BREAKPOINT_LG_PX}px)`);
  const handleDetailOpen = useCallback((agentId: number, open: boolean) => {
    setDetailOpenLanes((current) => {
      if (current.has(agentId) === open) return current;
      const next = new Set(current);
      if (open) next.add(agentId);
      else next.delete(agentId);
      return next;
    });
  }, []);
  const detailOpenCallbacks = useMemo(
    () => agents.map((agentId) => (open: boolean) => handleDetailOpen(agentId, open)),
    [agents, handleDetailOpen],
  );
  useEffect(() => {
    const stack = stackRef.current;
    if (!stack) return;
    const update = () => setStackWidth(stack.getBoundingClientRect().width);
    update();
    window.addEventListener("resize", update);
    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(update);
      observer.observe(stack);
    }
    return () => {
      window.removeEventListener("resize", update);
      observer?.disconnect();
    };
  }, []);
  const sharedWidth =
    stackWidth > 0
      ? sharedCanvasWidth(laneColumnWidth(stackWidth), detailOpenLanes.size > 0, wideLayout)
      : undefined;

  // The initial window is the configured fresh slice, the same one the single
  // view opens on; `undefined` means settings have not supplied it yet.
  const initialWindowOverride = useMemo<TimelineWindowOverride | null>(() => {
    if (settingsLoading || typeof window === "undefined") return null;
    return initialTimelineWindow(configuredWindowHours, new Date());
  }, [configuredWindowHours, settingsLoading]);
  const [selectedWindowOverride, setWindowOverride] = useState<
    TimelineWindowOverride | null | undefined
  >(undefined);
  const windowOverride =
    selectedWindowOverride === undefined ? initialWindowOverride : selectedWindowOverride;
  const windowReady = selectedWindowOverride !== undefined || initialWindowOverride !== null;

  const requestsBucketsUpfront = usesTimelineBuckets(windowOverride);
  const turnResults = useQueries({
    queries: agents.map((agentId) => ({
      queryKey: [
        "run-timeline",
        agentId,
        windowOverride?.from ?? null,
        windowOverride?.to ?? null,
        session,
        "turn",
      ],
      queryFn: () => api.getRunTimeline(agentId, { ...(windowOverride ?? {}), session }),
      enabled: windowReady && !requestsBucketsUpfront,
      placeholderData: keepPreviousData,
    })),
  });
  const spanMs = windowOverride
    ? Date.parse(windowOverride.to) - Date.parse(windowOverride.from)
    : 60_000;
  const bucketSeconds = pickBucketSeconds(spanMs, null);
  const maxTurns = Math.max(0, ...turnResults.map((result) => result.data?.meta.n_turns ?? 0));
  // One granularity for every lane: a lane that needs buckets switches them all
  // so the barrels stay comparable at the same bucket size.
  const shouldBucket = requestsBucketsUpfront || needsBuckets(maxTurns);
  const bucketResults = useQueries({
    queries: agents.map((agentId) => ({
      queryKey: [
        "run-timeline",
        agentId,
        windowOverride?.from ?? null,
        windowOverride?.to ?? null,
        session,
        "bucket",
        bucketLabel(bucketSeconds),
      ],
      queryFn: () =>
        api.getRunTimeline(agentId, {
          ...(windowOverride ?? {}),
          level: "bucket",
          bucket: `${bucketSeconds}s`,
          session,
        }),
      enabled: windowReady && shouldBucket,
      placeholderData: keepPreviousData,
    })),
  });
  const lanes = agents.map((agentId, index) => {
    const query = shouldBucket ? bucketResults[index] : turnResults[index];
    return { agentId, query, timeline: query.data };
  });

  const selectWindow = (next: TimelineWindowOverride) => setWindowOverride(next);
  // "Reset" here returns to the configured fresh slice (the window the page
  // opens on). The single view's reset means the whole session instead — a
  // compare view has no single "whole session", and alignment needs one
  // explicit shared window, so the two pages keep their own defensible
  // meaning (P3b may unify the control).
  const resetWindow = () => setWindowOverride(undefined);

  const setZoomWindow = (hours: number) => {
    const now = new Date();
    const presetSpanMs = hours * 60 * 60 * 1000;
    const next = windowOverride
      ? centerZoomWindow(
          windowOverride,
          presetSpanMs / (Date.parse(windowOverride.to) - Date.parse(windowOverride.from)),
          now,
        )
      : initialTimelineWindow(hours, now);
    selectWindow(next);
  };

  const zoomBy = (factor: number) => {
    if (!windowOverride) return;
    selectWindow(centerZoomWindow(windowOverride, factor, new Date()));
  };

  const selectSession = (nextSession: "compact" | "current") => {
    setSession(nextSession);
    resetWindow();
  };

  const drillBucket = (row: RunTimelineResponse["rows"][number]) => {
    if (!windowOverride || row.turn !== null) {
      throw new Error("Bucket drill-down requires an aggregated timeline row");
    }
    const drillTo = Math.min(
      Date.parse(row.start) + bucketSeconds * 1000,
      Date.parse(windowOverride.to),
    );
    selectWindow({ from: row.start, to: new Date(drillTo).toISOString() });
  };

  const toggleArrows = () => {
    setArrowsVisible((visible) => !visible);
    setHoveredArrow(null);
  };

  const fitTarget = overlapWindow(lanes.map((lane) => lane.timeline));
  const readout = hoveredArrow
    ? hoveredArrow.count > 1
      ? t("arrowClusterReadout", {
          source: hoveredArrow.sourceAgentId,
          target: hoveredArrow.targetAgentId,
          time: tickLabel(hoveredArrow.ts, false),
          count: hoveredArrow.count,
        })
      : t("arrowReadout", {
          source: hoveredArrow.sourceAgentId,
          target: hoveredArrow.targetAgentId,
          time: tickLabel(hoveredArrow.ts, false),
        })
    : t("arrowReadoutHint");

  return (
    <>
      <div className="sticky top-0 z-20">
        <div className="rounded border border-border bg-card p-1 shadow-sm">
          <div className={cn(FLEX, "flex-wrap items-center gap-1")}>
            <span className="max-w-full truncate px-1 font-mono text-[10px] text-muted-foreground">
              {windowOverride
                ? t("windowRange", {
                    from: dateTimeInputValue(windowOverride.from).replace("T", " "),
                    to: dateTimeInputValue(windowOverride.to).replace("T", " "),
                  })
                : null}
            </span>
            <div role="group" aria-label={t("zoom")} className={cn(FLEX, "items-center gap-1")}>
              {RUN_TIMELINE_ZOOM_HOURS.map((hours) => (
                <button
                  key={hours}
                  type="button"
                  onClick={() => setZoomWindow(hours)}
                  className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
                >
                  {zoomPresetLabel(hours)}
                </button>
              ))}
              <button
                type="button"
                aria-label={t("zoomOut")}
                onClick={() => zoomBy(2)}
                className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
              >
                −
              </button>
              <button
                type="button"
                aria-label={t("zoomIn")}
                onClick={() => zoomBy(0.5)}
                className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
              >
                +
              </button>
              <button
                type="button"
                aria-label={t("resetWindow")}
                onClick={resetWindow}
                className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
              >
                {t("resetWindow")}
              </button>
            </div>
            <button
              type="button"
              disabled={fitTarget === null}
              onClick={() => fitTarget && selectWindow(fitTarget)}
              className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted disabled:opacity-50"
            >
              {t("fitOverlap")}
            </button>
            <button
              type="button"
              aria-pressed={arrowsVisible}
              onClick={toggleArrows}
              className={cn(
                "rounded border px-2 py-1 font-mono text-xs",
                arrowsVisible
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border hover:bg-muted",
              )}
            >
              {t("arrowsToggle")}
            </button>
            <div role="group" aria-label={t("session")} className={cn(FLEX, "items-center gap-1")}>
              {(["compact", "current"] as const).map((route) => (
                <button
                  key={route}
                  type="button"
                  aria-pressed={session === route}
                  onClick={() => selectSession(route)}
                  className={cn(
                    "rounded border px-2 py-1 font-mono text-xs",
                    session === route
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border hover:bg-muted",
                  )}
                >
                  {route === "compact" ? t("compactSession") : t("currentSession")}
                </button>
              ))}
            </div>
            <span
              className={cn(
                FLEX_1,
                MIN_W_0,
                "truncate px-1 text-right font-mono text-[10px] text-muted-foreground",
              )}
            >
              {readout}
            </span>
          </div>
        </div>
      </div>

      <div ref={stackRef} className="relative space-y-4">
        {agents.map((agentId, index) => {
          const lane = lanes[index];
          const name = names.get(agentId);
          const dimmed =
            hoveredArrow !== null &&
            hoveredArrow.sourceAgentId !== agentId &&
            hoveredArrow.targetAgentId !== agentId;
          return (
            <div
              key={agentId}
              ref={laneRefs[index]}
              className={cn(FLEX, "gap-3 transition-opacity", dimmed && "opacity-40")}
            >
              <div className="w-28 shrink-0 pt-1">
                <Link
                  href={`/insights/run/${agentId}`}
                  title={t("laneOpenSingle")}
                  className={cn("block", MIN_W_0)}
                >
                  <span className={cn(FLEX, "items-center gap-1.5")}>
                    <span
                      className="size-2 shrink-0 rounded-full"
                      style={{ background: COMPARE_LANE_HUES[index % COMPARE_LANE_HUES.length] }}
                      aria-hidden
                    />
                    <span className="truncate text-xs font-semibold hover:underline">
                      {name ?? `#${agentId}`}
                    </span>
                  </span>
                  {name ? (
                    <span className="mt-0.5 block font-mono text-[10px] text-muted-foreground">
                      #{agentId}
                    </span>
                  ) : null}
                </Link>
              </div>
              <div className={cn(MIN_W_0, FLEX_1)}>
                {lane.timeline ? (
                  <RunTimelineChart
                    timeline={lane.timeline}
                    labels={labels}
                    onDrillBucket={drillBucket}
                    onZoomWindow={selectWindow}
                    showSummaries={showSummaries}
                    widthOverride={sharedWidth}
                    onDetailOpenChange={detailOpenCallbacks[index]}
                  />
                ) : lane.query.isPending ? (
                  <p className="font-mono text-sm text-muted-foreground">{t("loading")}</p>
                ) : (
                  <div className="space-y-2 font-mono text-sm text-destructive" role="alert">
                    <p>{t("loadFailed")}</p>
                    <button
                      type="button"
                      className={buttonVariants({ size: "sm" })}
                      onClick={() => void lane.query.refetch()}
                    >
                      {t("retry")}
                    </button>
                  </div>
                )}
              </div>
            </div>
          );
        })}
        {arrowsVisible && windowOverride ? (
          <CompareArrows
            containerRef={stackRef}
            laneRefs={laneRefs}
            lanes={lanes}
            timeWindow={windowOverride}
            hovered={hoveredArrow}
            onHoverChange={setHoveredArrow}
            label={t("arrowsOverlayLabel")}
          />
        ) : null}
      </div>
    </>
  );
}
