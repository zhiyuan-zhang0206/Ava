"use client";

import { useMemo } from "react";

import type { ResourcePoint } from "@/lib/types";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Simple SVG line-chart primitive — no external dependency.
// Renders an array of (x, y) points as a polyline + optional fill.
//
// The y-axis is an ABSOLUTE 0..100 percent scale (not auto-fit to the data's
// own min/max), so the trace sits where the reading actually is: 20% CPU draws
// near the bottom, 90% near the top. Auto-fitting made every series fill the
// full height regardless of load, so the shape looked like relative coordinates
// with no fixed reference.
// ---------------------------------------------------------------------------

interface LineChartProps {
  points: { x: number; y: number }[];
  width: number;
  height: number;
  stroke: string;
  fill?: string;
  /** Top-right label, e.g. "45%" */
  latestLabel?: string;
}

// Percent series share one fixed vertical domain so heights are comparable
// across CPU / memory and across time — the reading's position IS its value.
const Y_MAX = 100;

function SparkLine({ points, width, height, stroke, fill, latestLabel }: LineChartProps) {
  if (points.length < 2) {
    return (
      <svg width={width} height={height} className="block">
        <text
          x={width - 4}
          y={14}
          textAnchor="end"
          className="fill-muted-foreground text-[11px] tabular-nums"
        >
          {latestLabel ?? "—"}
        </text>
      </svg>
    );
  }

  // x → [0, width] over the sample window; y → [pad, height-pad] over a fixed
  // 0..100 percent domain (absolute, not normalized to the data's own range).
  const pad = 2;
  const plotH = height - pad * 2;
  const xs = points.map((p) => p.x);
  const xMin = xs[0];
  const xMax = xs[xs.length - 1];
  const xRange = xMax - xMin || 1;

  const scaleX = (v: number) => ((v - xMin) / xRange) * width;
  const scaleY = (v: number) => {
    const clamped = Math.max(0, Math.min(Y_MAX, v));
    return pad + plotH - (clamped / Y_MAX) * plotH;
  };

  const lineD = points.map((p, i) => `${i === 0 ? "M" : "L"}${scaleX(p.x)},${scaleY(p.y)}`).join(" ");

  let areaD = "";
  if (fill) {
    areaD = `${lineD} L${width},${height} L0,${height} Z`;
  }

  return (
    <svg width={width} height={height} className="block overflow-visible">
      {fill && <path d={areaD} fill={fill} />}
      <path d={lineD} fill="none" stroke={stroke} strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" />
      {latestLabel && (
        <text
          x={width - 4}
          y={14}
          textAnchor="end"
          className="fill-muted-foreground text-[11px] tabular-nums"
        >
          {latestLabel}
        </text>
      )}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// ResourceChart — one machine's CPU / memory / disk on a single row
// ---------------------------------------------------------------------------

interface ResourceChartProps {
  history: ResourcePoint[];
  compact?: boolean;
}

function formatGb(n: number): string {
  return n < 10 ? n.toFixed(1) : String(Math.round(n));
}

export function ResourceChart({ history, compact = false }: ResourceChartProps) {
  const { cpuPoints, memPoints, latest } = useMemo(() => {
    if (history.length === 0) {
      return { cpuPoints: [], memPoints: [], latest: null };
    }
    const cpuPts = history.map((p) => ({ x: p.ts, y: p.cpu_pct }));
    const memPts = history.map((p) => ({ x: p.ts, y: p.mem_pct }));
    const last = history[history.length - 1];
    return { cpuPoints: cpuPts, memPoints: memPts, latest: last };
  }, [history]);

  if (!latest) {
    return <p className="text-xs text-muted-foreground">No resource data</p>;
  }

  const w = compact ? 140 : 200;
  const h = compact ? 40 : 48;

  return (
    <div className={cn("flex-nowrap items-center gap-4 overflow-x-auto text-xs", FLEX)}>
      {/* CPU — time series on a fixed 0..100 scale */}
      <div className={cn("items-center gap-1.5", FLEX)}>
        <span className="text-muted-foreground w-8 shrink-0">CPU</span>
        <SparkLine
          points={cpuPoints}
          width={w}
          height={h}
          stroke="hsl(217 91% 60%)"
          fill="hsla(217 91% 60% / 0.08)"
          latestLabel={`${Math.round(latest.cpu_pct)}%`}
        />
      </div>

      {/* Memory — time series on a fixed 0..100 scale */}
      <div className={cn("items-center gap-1.5", FLEX)}>
        <span className="text-muted-foreground w-8 shrink-0">Memory</span>
        <SparkLine
          points={memPoints}
          width={w}
          height={h}
          stroke="hsl(142 71% 45%)"
          fill="hsla(142 71% 45% / 0.08)"
          latestLabel={`${Math.round(latest.mem_pct)}%`}
        />
        <span className="text-muted-foreground/70 tabular-nums whitespace-nowrap">
          {formatGb(latest.mem_used_gb)}/{formatGb(latest.mem_total_gb)}GB
        </span>
      </div>

      {/* Disk — static current-usage metric, no trend (it barely moves over the
          ~25 min window and the number matters more than the shape). */}
      <div className={cn("items-center gap-1.5", FLEX)}>
        <span className="text-muted-foreground w-8 shrink-0">Disk</span>
        <span className="tabular-nums font-medium whitespace-nowrap">
          {Math.round(latest.disk_pct)}%
        </span>
        <span className="text-muted-foreground/70 tabular-nums whitespace-nowrap">
          {formatGb(latest.disk_used_gb)}/{formatGb(latest.disk_total_gb)}GB
        </span>
      </div>
    </div>
  );
}
