"use client";

// The multi-agent view of one agent tree: cluster curves over a time axis shared with the agent
// lanes below them, message connectors between lanes, and any one lane opened in place into the
// agent's own run timeline. The window the user asked for is the extent; zoom and pan move a viewport
// inside it, and the reads follow the viewport (a window a little wider than the view, finer when
// zoomed in) so the bucket width and the lanes' detail match what is on screen.

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";

import { RunTimelineAxis } from "@/components/run-timeline/run-timeline-axis";
import { buildAxisMap, clampViewport, viewportOf, type Viewport } from "@/components/run-timeline/timeline-model";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { api } from "@/lib/transport/api";

import { useTrackWidth, useViewGestures } from "./cluster-gestures";
import { ClusterCurvesRows } from "./cluster-curves";
import { ClusterLanesRows } from "./cluster-lanes";
import { agentColors, formatUsd, nextFetchWindow, windowIso } from "./cluster-model";

/** How long the view must rest before the reads follow it. */
const SETTLE_MS = 250;

export interface ClusterViewProps {
  root: number;
  /** The extent: ISO times of the window the user asked for. */
  window: { from: string; to: string };
}

export function ClusterView({ root, window: extent }: ClusterViewProps) {
  const t = useTranslations("clusterView");
  const base = useMemo<Viewport>(() => viewportOf(extent), [extent]);
  const [view, setView] = useState<Viewport>(base);
  const [loaded, setLoaded] = useState<Viewport>(() => nextFetchWindow(null, base, base));
  const [level, setLevel] = useState<number | null>(null);
  const [folded, setFolded] = useState<ReadonlySet<number>>(new Set());
  const [open, setOpen] = useState<number | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  const chartRef = useRef<HTMLDivElement>(null);
  const trackPx = useTrackWidth(chartRef);
  const shownView = clampViewport(view, base);
  const gestures = useViewGestures(chartRef, shownView, base, setView);

  useEffect(() => {
    const timer = setTimeout(() => setLoaded((previous) => nextFetchWindow(previous, shownView, base)), SETTLE_MS);
    return () => clearTimeout(timer);
  }, [shownView.from, shownView.to, base]); // eslint-disable-line react-hooks/exhaustive-deps -- the view's two ends are its identity

  const range = windowIso(loaded);
  const query = { root, ...range };
  const curves = useQuery({
    queryKey: ["cluster-curves", root, range.from, range.to],
    queryFn: () => api.getClusterCurves(query),
    placeholderData: keepPreviousData,
  });
  const lanes = useQuery({
    queryKey: ["cluster-lanes", root, range.from, range.to, level],
    queryFn: () => api.getClusterLanes(query, level),
    placeholderData: keepPreviousData,
  });
  const messages = useQuery({
    queryKey: ["cluster-messages", root, range.from, range.to],
    queryFn: () => api.getClusterMessages(query),
    placeholderData: keepPreviousData,
  });

  const laneList = useMemo(() => lanes.data?.lanes ?? [], [lanes.data]);
  const colors = useMemo(() => agentColors(laneList), [laneList]);
  const order = useMemo(() => laneList.map((lane) => lane.agent_id), [laneList]);
  const axis = useMemo(() => buildAxisMap([], base, "time"), [base]);

  if (lanes.isError && lanes.data === undefined) {
    return (
      <div className="space-y-2 font-mono text-sm text-destructive" role="alert">
        <p>{t("loadFailed", { message: lanes.error instanceof Error ? lanes.error.message : "" })}</p>
        <button type="button" className={buttonVariants({ size: "sm" })} onClick={() => void lanes.refetch()}>
          {t("retry")}
        </button>
      </div>
    );
  }

  const total = laneList.reduce((sum, lane) => sum + lane.cost_usd, 0);
  const foldable = laneList.filter((lane, index) => laneList[index + 1]?.depth > lane.depth && lane.depth === 0);
  const toggleFold = (agentId: number) =>
    setFolded((previous) => {
      const next = new Set(previous);
      if (!next.delete(agentId)) next.add(agentId);
      return next;
    });

  return (
    <div className="space-y-3" data-testid="cluster-view">
      <div className={cn(FLEX, "flex-wrap items-center gap-3 text-xs text-muted-foreground")}>
        <span data-testid="cluster-summary">
          {t("summary", { agents: laneList.length, cost: formatUsd(total) })}
        </span>
        <label className={cn(FLEX, "items-center gap-1")}>
          {t("level")}
          <select
            aria-label={t("level")}
            value={level ?? ""}
            onChange={(event) => setLevel(event.target.value === "" ? null : Number(event.target.value))}
            className="rounded border border-border bg-background px-1 py-0.5 font-mono"
          >
            <option value="">{t("levelAuto", { level: lanes.data?.level ?? "-" })}</option>
            {(lanes.data?.levels ?? []).map((entry) => (
              <option key={entry.level} value={entry.level}>
                {t("levelOption", { level: entry.level, nodes: entry.nodes })}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="rounded border border-border px-1.5 py-0.5"
          onClick={() => setFolded(new Set(foldable.map((lane) => lane.agent_id)))}
        >
          {t("foldAll")}
        </button>
        <button type="button" className="rounded border border-border px-1.5 py-0.5" onClick={() => setFolded(new Set())}>
          {t("unfoldAll")}
        </button>
        {messages.data?.truncated ? (
          <span role="status">{t("messagesTruncated", { shown: messages.data.edges.length, total: messages.data.total })}</span>
        ) : null}
        {curves.data && curves.data.unpriced_calls > 0 ? (
          <span role="status">{t("unpriced", { count: curves.data.unpriced_calls })}</span>
        ) : null}
      </div>
      <div
        ref={chartRef}
        role="group"
        aria-label={t("chartAria")}
        data-testid="cluster-chart"
        {...gestures}
        className="relative select-none space-y-1.5 rounded-[10px] border border-border bg-card p-3"
      >
        <p
          data-testid="cluster-readout"
          className={cn(MIN_W_0, "h-4 truncate pl-[88px] font-mono text-[10px] text-muted-foreground")}
        >
          {hint ?? t("readoutIdle")}
        </p>
        <ClusterCurvesRows
          curves={curves.data}
          order={order}
          colors={colors}
          view={shownView}
          trackPx={trackPx}
          onHover={setHint}
        />
        <RunTimelineAxis view={shownView} base={base} onView={setView} axis={axis} trackPx={trackPx} />
        {lanes.data ? (
          <ClusterLanesRows
            lanes={lanes.data}
            messages={messages.data}
            view={shownView}
            base={base}
            onView={setView}
            loaded={loaded}
            trackPx={trackPx}
            colors={colors}
            folded={folded}
            onFold={toggleFold}
            open={open}
            onOpen={(agentId) => setOpen((previous) => (previous === agentId ? null : agentId))}
            onHint={setHint}
          />
        ) : (
          <div className="h-32 animate-pulse rounded bg-muted/40" aria-hidden="true" />
        )}
      </div>
    </div>
  );
}
