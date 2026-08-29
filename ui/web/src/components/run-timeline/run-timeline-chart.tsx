"use client";

import { useState } from "react";

import { formatTokensCompact } from "@/lib/item-summary";
import { FLEX } from "@/lib/layout";
import type { RunTimelineResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

import { timeCoordinate, tokenBarWidth } from "./scales";

const SVG_WIDTH = 1000;
const PLOT_LEFT = 44;
const PLOT_WIDTH = 930;
const TIME_Y = 54;
const TOKEN_Y = 208;
const TOKEN_PANEL_MIN_HEIGHT = 52;
const TOKEN_PANEL_MAX_HEIGHT = 600;
const BAR_HEIGHT = 28;
const IDLE_LABEL_MIN_WIDTH = 55;
const DENSE_ROW_THRESHOLD = 40;
const EVENT_RAIL_LIMIT = 120;
const EVENT_RAIL_PRIORITY = new Set([
  "exec_failed",
  "exec(failed)",
  "exec_timeout",
  "exec(timeout)",
  "llm_provider_error",
  "stream_stalled_retry",
  "llm_turn_aborted",
  "compact",
  "auto_compact",
  "restart_completed",
  "resurrect",
  "agent_terminated",
]);

export interface RunTimelineChartLabels {
  chart: string;
  waterfall: string;
  time: string;
  tokens: string;
  eventRail: string;
  input: string;
  output: string;
  idle: string;
  turn: string;
  bucket: string;
  cost: string;
  model: string;
  empty: string;
  noTokenData: string;
  moreEvents: (count: number, summary: string) => string;
}

function rowLabel(row: RunTimelineResponse["rows"][number], labels: RunTimelineChartLabels): string {
  return row.turn === null ? `${labels.bucket} (${row.n_turns})` : `${labels.turn} ${row.turn}`;
}

function idleLabel(tags: readonly string[], labels: RunTimelineChartLabels): string | null {
  const tag = tags.find((candidate) => candidate.startsWith("idle_before_"));
  if (!tag) return null;
  const seconds = Number(tag.slice("idle_before_".length, -1));
  if (!Number.isFinite(seconds)) return labels.idle;
  if (seconds < 60) return `${labels.idle} ${seconds}s`;
  if (seconds < 3600) return `${labels.idle} ${Math.round(seconds / 60)}m`;
  return `${labels.idle} ${(seconds / 3600).toFixed(1)}h`;
}

function currency(amount: number): string {
  return `$${amount.toFixed(amount < 0.01 ? 4 : 2)}`;
}

function eventChipClass(kind: string): string {
  if (kind.includes("failed") || kind.includes("timeout")) {
    return "rounded border border-destructive/50 bg-destructive/10 px-1.5 py-0.5 text-[10px] text-destructive";
  }
  if (kind === "compact" || kind === "auto_compact") {
    return "rounded border border-violet-500/50 bg-violet-500/10 px-1.5 py-0.5 text-[10px] text-violet-700 dark:text-violet-300";
  }
  if (kind.includes("restart") || kind.includes("resurrect")) {
    return "rounded border border-blue-500/50 bg-blue-500/10 px-1.5 py-0.5 text-[10px] text-blue-700 dark:text-blue-300";
  }
  if (kind === "halt") {
    return "rounded border border-muted-foreground/30 bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground";
  }
  return "rounded border border-border bg-background px-1.5 py-0.5 text-[10px] text-muted-foreground";
}

function prioritizedRailEvents(events: RunTimelineResponse["events"]) {
  const indexed = events.map((event, index) => ({ event, index }));
  const selected = [...indexed.filter(({ event }) => EVENT_RAIL_PRIORITY.has(event.kind)), ...indexed.filter(({ event }) => !EVENT_RAIL_PRIORITY.has(event.kind))].slice(
    0,
    EVENT_RAIL_LIMIT,
  );
  const selectedIndexes = new Set(selected.map(({ index }) => index));
  const skippedByKind = new Map<string, number>();
  for (const { event, index } of indexed) {
    if (!selectedIndexes.has(index)) {
      skippedByKind.set(event.kind, (skippedByKind.get(event.kind) ?? 0) + 1);
    }
  }
  return {
    events: selected.map(({ event }) => event),
    skippedCount: events.length - selected.length,
    skippedSummary: [...skippedByKind.entries()]
      .map(([kind, count]) => `${kind}×${count}`)
      .join(", "),
  };
}

/**
 * Two independent horizontal scales share ordinal correspondence markers.  The
 * dashed vertical lines connect those markers rather than implying that time
 * and token x-coordinates are interchangeable measurements.
 */
export function RunTimelineChart({
  timeline,
  labels,
}: {
  timeline: RunTimelineResponse;
  labels: RunTimelineChartLabels;
}) {
  const [hovered, setHovered] = useState<RunTimelineResponse["rows"][number] | null>(null);
  const rows = timeline.rows;
  if (rows.length === 0) {
    return (
      <section className="rounded border border-border bg-card p-3" aria-label={labels.chart}>
        <p className="font-mono text-sm text-muted-foreground">{labels.empty}</p>
      </section>
    );
  }

  const maximumTokens = Math.max(1, ...rows.map((row) => row.llm.in_total));
  const tokenRowHeight = Math.max(2, Math.min(28, TOKEN_PANEL_MAX_HEIGHT / rows.length));
  const tokenPanelHeight = Math.max(
    TOKEN_PANEL_MIN_HEIGHT,
    Math.min(TOKEN_PANEL_MAX_HEIGHT, tokenRowHeight * rows.length),
  );
  const chartHeight = TOKEN_Y + tokenPanelHeight + 26;
  const rail = prioritizedRailEvents(timeline.events);
  const allTokenDataUnmatched =
    timeline.meta.n_turns > 0 &&
    timeline.meta.unmatched_turns >= timeline.meta.n_turns &&
    timeline.meta.tokens_in === 0 &&
    timeline.meta.tokens_out === 0;
  const correspondenceX = (index: number) =>
    PLOT_LEFT + ((index + 0.5) / rows.length) * PLOT_WIDTH;

  return (
    <section className="space-y-2 rounded border border-border bg-card p-3" aria-label={labels.chart}>
      <div data-testid="run-timeline-scroll" className="overflow-x-auto">
        <svg
          viewBox={`0 0 ${SVG_WIDTH} ${chartHeight}`}
          className="h-auto min-w-[640px] w-full font-mono text-[11px]"
          role="img"
          aria-label={labels.waterfall}
        >
        <g aria-label={`${labels.time} panel`}>
          <text x={PLOT_LEFT} y="20" className="fill-muted-foreground font-sans text-xs font-semibold">
            {labels.time}
          </text>
          <line x1={PLOT_LEFT} x2={PLOT_LEFT + PLOT_WIDTH} y1="88" y2="88" className="stroke-border" />
          {rows.map((row, index) => {
            const x = PLOT_LEFT + timeCoordinate(row.start, timeline.window.from, timeline.window.to, PLOT_WIDTH);
            const end = PLOT_LEFT + timeCoordinate(row.end, timeline.window.from, timeline.window.to, PLOT_WIDTH);
            const width = Math.max(3, end - x);
            const duration = Math.max(0.001, Date.parse(row.end) - Date.parse(row.start));
            const activeWidth = Math.min(width, Math.max(2, width * ((row.active_s * 1000) / duration)));
            const idle = idleLabel(row.tags, labels);
            const nextX =
              index + 1 < rows.length
                ? PLOT_LEFT + timeCoordinate(rows[index + 1].start, timeline.window.from, timeline.window.to, PLOT_WIDTH)
                : PLOT_LEFT + PLOT_WIDTH;
            return (
              <g key={`${row.turn ?? "bucket"}-${index}`}>
                <rect
                  x={x}
                  y={TIME_Y}
                  width={width}
                  height={BAR_HEIGHT}
                  rx="3"
                  className={cn("fill-muted/70", row.anomalies.length > 0 ? "stroke-destructive" : "stroke-border")}
                />
                <rect
                  x={x}
                  y={TIME_Y}
                  width={activeWidth}
                  height={BAR_HEIGHT}
                  rx="3"
                  className="fill-foreground/75"
                >
                  <title>{`${rowLabel(row, labels)} · ${row.active_s.toFixed(1)}s active`}</title>
                </rect>
                {idle && rows.length <= DENSE_ROW_THRESHOLD && nextX - x >= IDLE_LABEL_MIN_WIDTH ? (
                  <text
                    x={x}
                    y="104"
                    className="fill-muted-foreground [paint-order:stroke] stroke-card stroke-[3px]"
                  >
                    {idle}
                  </text>
                ) : null}
                <circle cx={correspondenceX(index)} cy="98" r="2.5" className="fill-primary" />
              </g>
            );
          })}
        </g>

        {rows.map((row, index) => (
          <line
            key={`${row.turn ?? "bucket"}-${index}`}
            data-testid="run-connector"
            x1={correspondenceX(index)}
            x2={correspondenceX(index)}
            y1="100"
            y2="197"
            className="stroke-muted-foreground"
            strokeDasharray="4 4"
            strokeOpacity="0.75"
          />
        ))}

        <g aria-label={`${labels.tokens} panel`}>
          <text x={PLOT_LEFT} y="170" className="fill-muted-foreground font-sans text-xs font-semibold">
            {labels.tokens}
          </text>
          <text x={PLOT_LEFT + PLOT_WIDTH} y="170" textAnchor="end" className="fill-muted-foreground">
            {labels.input}: 0 – {formatTokensCompact(maximumTokens)}
          </text>
          <line
            x1={PLOT_LEFT}
            x2={PLOT_LEFT + PLOT_WIDTH}
            y1={TOKEN_Y + tokenPanelHeight + 4}
            y2={TOKEN_Y + tokenPanelHeight + 4}
            className="stroke-border"
          />
          {rows.map((row, index) => {
            const inputWidth = tokenBarWidth(row.llm.in_total, maximumTokens, PLOT_WIDTH);
            const outputWidth = tokenBarWidth(row.llm.out_total, maximumTokens, PLOT_WIDTH);
            const label = rowLabel(row, labels);
            const y = TOKEN_Y + index * tokenRowHeight;
            const height = Math.max(1, tokenRowHeight - 1);
            return (
              <g key={`${row.turn ?? "bucket"}-${index}`}>
                <rect
                  aria-label={`${label} token bar`}
                  x={PLOT_LEFT}
                  y={y}
                  width={inputWidth}
                  height={height}
                  rx="3"
                  className="fill-violet-500/75"
                  onMouseEnter={() => setHovered(row)}
                  onMouseLeave={() => setHovered(null)}
                >
                  <title>{`${label} · ${row.llm.in_total.toLocaleString()} ${labels.input}`}</title>
                </rect>
                <rect
                  x={PLOT_LEFT}
                  y={y}
                  width={outputWidth}
                  height={Math.min(5, height)}
                  rx="2"
                  className="fill-cyan-400"
                  pointerEvents="none"
                />
                {height >= 14 ? (
                  <text x={PLOT_LEFT + 4} y={y + Math.min(height - 5, 19)} className="fill-primary-foreground">
                    {formatTokensCompact(row.llm.in_total)}
                  </text>
                ) : null}
                <circle cx={correspondenceX(index)} cy="197" r="2.5" className="fill-primary" />
              </g>
            );
          })}
        </g>
          {allTokenDataUnmatched ? (
            <text x={PLOT_LEFT} y={TOKEN_Y + tokenPanelHeight + 20} className="fill-amber-600 dark:fill-amber-400">
              {labels.noTokenData}
            </text>
          ) : null}
        </svg>
      </div>

      <div aria-label={labels.eventRail} className={cn(FLEX, "flex-wrap items-center gap-2 border-t border-border pt-2 font-mono text-[11px]")}>
        <span className="text-muted-foreground">{labels.eventRail}</span>
        {timeline.events.length === 0 ? <span className="text-muted-foreground">—</span> : null}
        {rail.events.map((event, index) => (
          <span
            key={`${event.kind}-${event.ts}-${index}`}
            data-testid="event-chip"
            className={eventChipClass(event.kind)}
            title={event.label ?? event.ts}
          >
            {event.kind}
          </span>
        ))}
        {rail.skippedCount > 0 ? (
          <span className="text-muted-foreground">{labels.moreEvents(rail.skippedCount, rail.skippedSummary)}</span>
        ) : null}
      </div>

      {hovered ? (
        <div role="tooltip" className="rounded bg-muted px-2 py-1 font-mono text-[11px] text-muted-foreground">
          {rowLabel(hovered, labels)} · {labels.input} {hovered.llm.in_total.toLocaleString()} · {labels.output}{" "}
          {hovered.llm.out_total.toLocaleString()} · {labels.cost} {currency(hovered.llm.cost_usd)} · {labels.model}{" "}
          {hovered.llm.model ?? "—"}
        </div>
      ) : null}
    </section>
  );
}
