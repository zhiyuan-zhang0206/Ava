"use client";

// The cluster curves on the shared time axis: cost per minute stacked by agent, active agents,
// messages per minute and queue time. One column per bucket of the loaded data; the columns sit at
// their own times, so data fetched for an earlier window stays right while a newer one loads.

import { useTranslations } from "next-intl";

import type { ClusterCurves, CurveBucket } from "@/lib/contracts/types";

import type { Viewport } from "../run-timeline/timeline-model";
import { RowShell } from "../run-timeline/run-timeline-row-shell";
import {
  activeSeries,
  costSegments,
  formatSeconds,
  formatUsd,
  messageSeries,
  niceMax,
  plotY,
  queueSeries,
  timeX,
  type BucketValue,
} from "./cluster-model";

const PLOT_HEIGHT = 56;
const ROW_HEIGHT = "h-14";
// A column is never thinner than this, so a bucket stays visible when zoomed far out.
const MIN_COLUMN_PX = 1;

function column(x0: number, x1: number, view: Viewport, trackPx: number): { x: number; width: number } | null {
  if (x1 < view.from || x0 > view.to) return null;
  const left = timeX(x0, view, trackPx);
  const right = timeX(x1, view, trackPx);
  return { x: left, width: Math.max(right - left - 0.5, MIN_COLUMN_PX) };
}

function Plot({
  trackPx,
  max,
  maxLabel,
  hover,
  children,
}: {
  trackPx: number;
  max: number;
  maxLabel: string;
  hover: { onMouseMove: (event: React.MouseEvent<SVGSVGElement>) => void; onMouseLeave: () => void };
  children: React.ReactNode;
}) {
  return (
    <>
      <svg
        width={trackPx}
        height={PLOT_HEIGHT}
        className="block"
        aria-hidden="true"
        data-max={max}
        {...hover}
      >
        {children}
      </svg>
      <span className="pointer-events-none absolute left-1 top-0 font-mono text-[9px] text-muted-foreground">
        {maxLabel}
      </span>
    </>
  );
}

function Bars({
  series,
  view,
  trackPx,
  max,
  fill,
}: {
  series: readonly BucketValue[];
  view: Viewport;
  trackPx: number;
  max: number;
  fill: string;
}) {
  return (
    <>
      {series.map((point) => {
        const box = column(point.x0, point.x1, view, trackPx);
        if (box === null) return null;
        const top = plotY(point.value, max, PLOT_HEIGHT);
        return <rect key={point.x0} x={box.x} y={top} width={box.width} height={PLOT_HEIGHT - top} fill={fill} />;
      })}
    </>
  );
}

/** The bucket holding an instant, if the loaded data has one there. */
export function bucketAt(curves: ClusterCurves, ms: number): CurveBucket | undefined {
  const widthMs = curves.bucket_seconds * 1000;
  return curves.buckets.find((bucket) => {
    const start = Date.parse(bucket.ts);
    return ms >= start && ms < start + widthMs;
  });
}

export function ClusterCurvesRows({
  curves,
  order,
  colors,
  view,
  trackPx,
  onHover,
}: {
  curves: ClusterCurves | undefined;
  /** Agent ids in lane order: the stacking order of the cost bars. */
  order: readonly number[];
  colors: ReadonlyMap<number, string>;
  view: Viewport;
  trackPx: number;
  /** The text to read out for the pointer's bucket, null when it leaves. */
  onHover: (text: string | null) => void;
}) {
  const t = useTranslations("clusterView");
  if (curves === undefined) {
    return (
      <div className="h-16 animate-pulse rounded bg-muted/40" data-testid="cluster-curves-loading" aria-hidden="true" />
    );
  }
  const cost = costSegments(curves, order);
  const costMax = niceMax(cost.max);
  const active = activeSeries(curves);
  const activeMax = niceMax(active.reduce((top, point) => Math.max(top, point.value), 0));
  const messages = messageSeries(curves);
  const messagesMax = niceMax(messages.reduce((top, point) => Math.max(top, point.value), 0));
  const queue = queueSeries(curves);
  const queueMax = niceMax(queue.reduce((top, point) => Math.max(top, point.p95 ?? point.p50 ?? 0), 0));

  const readout = (event: React.MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const ms = view.from + ((event.clientX - rect.left) / rect.width) * (view.to - view.from);
    const bucket = bucketAt(curves, ms);
    if (bucket === undefined) {
      onHover(null);
      return;
    }
    const spend = bucket.costs.reduce((sum, entry) => sum + entry.cost_usd, 0);
    onHover(
      t("bucketReadout", {
        time: new Date(Date.parse(bucket.ts)).toLocaleTimeString(),
        seconds: curves.bucket_seconds,
        cost: formatUsd((spend * 60) / curves.bucket_seconds),
        active: bucket.active_agents,
        messages: bucket.messages,
        queue:
          bucket.queue_samples > 0
            ? t("queueReadout", {
                p50: formatSeconds(bucket.queue_p50_seconds ?? 0),
                p95: formatSeconds(bucket.queue_p95_seconds ?? 0),
                samples: bucket.queue_samples,
              })
            : t("queueNone"),
      }),
    );
  };
  const hoverProps = { onMouseMove: readout, onMouseLeave: () => onHover(null) };

  return (
    <div className="space-y-1.5" data-testid="cluster-curves">
      <RowShell label={t("curveCost")} height={ROW_HEIGHT} testId="cluster-curve-cost">
        <Plot hover={hoverProps} trackPx={trackPx} max={costMax} maxLabel={`${formatUsd(costMax)}/min`}>
          {cost.segments.map((segment) => {
            const box = column(segment.x0, segment.x1, view, trackPx);
            if (box === null) return null;
            const top = plotY(segment.y1, costMax, PLOT_HEIGHT);
            const bottom = plotY(segment.y0, costMax, PLOT_HEIGHT);
            return (
              <rect
                key={`${segment.x0}-${segment.agentId}`}
                x={box.x}
                y={top}
                width={box.width}
                height={Math.max(bottom - top, 0.5)}
                fill={colors.get(segment.agentId) ?? "var(--muted-foreground)"}
              />
            );
          })}
        </Plot>
      </RowShell>
      <RowShell label={t("curveActive")} height={ROW_HEIGHT} testId="cluster-curve-active">
        <Plot hover={hoverProps} trackPx={trackPx} max={activeMax} maxLabel={String(activeMax)}>
          <Bars series={active} view={view} trackPx={trackPx} max={activeMax} fill="var(--primary)" />
        </Plot>
      </RowShell>
      <RowShell label={t("curveMessages")} height={ROW_HEIGHT} testId="cluster-curve-messages">
        <Plot hover={hoverProps} trackPx={trackPx} max={messagesMax} maxLabel={`${messagesMax}/min`}>
          <Bars series={messages} view={view} trackPx={trackPx} max={messagesMax} fill="var(--primary)" />
        </Plot>
      </RowShell>
      <RowShell label={t("curveQueue")} height={ROW_HEIGHT} testId="cluster-curve-queue">
        <Plot hover={hoverProps} trackPx={trackPx} max={queueMax} maxLabel={formatSeconds(queueMax)}>
          <Bars
            series={queue.map((point) => ({ x0: point.x0, x1: point.x1, value: point.p95 ?? 0 }))}
            view={view}
            trackPx={trackPx}
            max={queueMax}
            fill="color-mix(in srgb, var(--primary) 35%, transparent)"
          />
          <Bars
            series={queue.map((point) => ({ x0: point.x0, x1: point.x1, value: point.p50 ?? 0 }))}
            view={view}
            trackPx={trackPx}
            max={queueMax}
            fill="var(--primary)"
          />
        </Plot>
      </RowShell>
    </div>
  );
}
