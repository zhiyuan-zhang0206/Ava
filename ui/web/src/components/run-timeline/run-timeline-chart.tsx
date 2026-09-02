"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { FLEX, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout";
import type { RunTimelineResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

import { buildTimelineLayout } from "./timeline-layout";

const MIN_CANVAS_WIDTH = 1000;
const MIN_DETAIL_CANVAS_WIDTH = 320;
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
  visualization: string;
  time: string;
  eventRail: string;
  input: string;
  output: string;
  turn: string;
  bucket: string;
  cost: string;
  model: string;
  empty: string;
  moreEvents: (count: number, summary: string) => string;
  turnDetails: string;
  timeRange: string;
  activeSeconds: string;
  latency: string;
  executions: string;
  tool: string;
  duration: string;
  status: string;
  succeeded: string;
  failed: string;
  anomalies: string;
  none: string;
  noExecutions: string;
  closeDetails: string;
}

function rowLabel(row: RunTimelineResponse["rows"][number], labels: RunTimelineChartLabels): string {
  return row.turn === null ? `${labels.bucket} (${row.n_turns})` : `${labels.turn} ${row.turn}`;
}

function currency(amount: number): string {
  return `$${amount.toFixed(amount < 0.01 ? 4 : 2)}`;
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function datePart(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function timePart(date: Date): string {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function timeRange(startValue: string, endValue: string): string {
  const start = new Date(startValue);
  const end = new Date(endValue);
  const endLabel = datePart(start) === datePart(end) ? timePart(end) : `${datePart(end)} ${timePart(end)}`;
  return `${datePart(start)} ${timePart(start)} – ${endLabel}`;
}

function tickLabel(timestamp: string): string {
  const date = new Date(timestamp);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function rowFailed(row: RunTimelineResponse["rows"][number]): boolean {
  return row.ok === false || row.execs.some((execution) => !execution.ok) || row.anomalies.length > 0;
}

function eventChipClass(kind: string): string {
  if (kind.includes("failed") || kind.includes("timeout")) {
    return "border-[var(--series-5)] bg-red-50 text-red-700";
  }
  if (kind === "compact" || kind === "auto_compact") {
    return "border-violet-300 bg-violet-50 text-violet-700";
  }
  if (kind.includes("restart") || kind.includes("resurrect")) {
    return "border-blue-300 bg-blue-50 text-blue-700";
  }
  return "border-[#e7e7e9] bg-white text-neutral-600";
}

function prioritizedRailEvents(events: RunTimelineResponse["events"]) {
  const indexed = events.map((event, index) => ({ event, index }));
  const selected = [
    ...indexed.filter(({ event }) => EVENT_RAIL_PRIORITY.has(event.kind)),
    ...indexed.filter(({ event }) => !EVENT_RAIL_PRIORITY.has(event.kind)),
  ].slice(0, EVENT_RAIL_LIMIT);
  const selectedIndexes = new Set(selected.map(({ index }) => index));
  const skippedByKind = new Map<string, number>();
  for (const { event, index } of indexed) {
    if (!selectedIndexes.has(index)) {
      skippedByKind.set(event.kind, (skippedByKind.get(event.kind) ?? 0) + 1);
    }
  }
  selected.sort((left, right) => Date.parse(left.event.ts) - Date.parse(right.event.ts));
  return {
    events: selected.map(({ event }) => event),
    skippedCount: events.length - selected.length,
    skippedSummary: [...skippedByKind.entries()]
      .map(([kind, count]) => `${kind}×${count}`)
      .join(", "),
  };
}

function DetailMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-1 rounded-[10px] border border-[#e7e7e9] bg-neutral-50 px-3 py-2">
      <dt className="text-[10px] font-medium uppercase tracking-wide text-neutral-500">{label}</dt>
      <dd className="font-mono text-xs tabular-nums text-neutral-900">{value}</dd>
    </div>
  );
}

function TurnDetailPanel({
  row,
  labels,
  onClose,
}: {
  row: RunTimelineResponse["rows"][number];
  labels: RunTimelineChartLabels;
  onClose: () => void;
}) {
  return (
    <aside
      role="region"
      aria-label={labels.turnDetails}
      className="h-fit space-y-4 rounded-[10px] border border-[#e7e7e9] bg-white p-4 text-neutral-900"
    >
      <header className={cn(FLEX, "items-start justify-between gap-3")}>
        <div>
          <p className="text-[10px] font-medium uppercase tracking-wide text-neutral-500">{labels.turnDetails}</p>
          <h3 className="text-sm font-semibold">{rowLabel(row, labels)}</h3>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label={labels.closeDetails}
          className="rounded-md px-1.5 py-0.5 text-neutral-500 hover:bg-neutral-100 hover:text-neutral-900"
        >
          ×
        </button>
      </header>

      <dl className="grid grid-cols-2 gap-2">
        <div className="col-span-2">
          <DetailMetric label={labels.timeRange} value={timeRange(row.start, row.end)} />
        </div>
        <DetailMetric label={labels.activeSeconds} value={`${row.active_s.toFixed(1)}s`} />
        <DetailMetric label={labels.latency} value={`${(row.llm.latency_ms / 1000).toFixed(2)}s`} />
        <DetailMetric label={labels.input} value={row.llm.in_total.toLocaleString()} />
        <DetailMetric label={labels.output} value={row.llm.out_total.toLocaleString()} />
        <DetailMetric label={labels.cost} value={currency(row.llm.cost_usd)} />
        <DetailMetric label={labels.model} value={row.llm.model ?? "—"} />
        <div className="col-span-2">
          <DetailMetric label={labels.status} value={rowFailed(row) ? labels.failed : labels.succeeded} />
        </div>
      </dl>

      <section className="space-y-2">
        <h4 className="text-xs font-semibold">{labels.executions}</h4>
        {row.execs.length === 0 ? (
          <p className="text-xs text-neutral-500">{labels.noExecutions}</p>
        ) : (
          <div className={cn(OVERFLOW_HIDDEN, "rounded-[10px] border border-[#e7e7e9]")}>
            <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] gap-3 bg-neutral-50 px-3 py-2 text-[10px] font-medium uppercase tracking-wide text-neutral-500">
              <span>{labels.tool}</span>
              <span>{labels.duration}</span>
              <span>{labels.status}</span>
            </div>
            {row.execs.map((execution, index) => (
              <div
                key={`${execution.tool}-${index}`}
                className="grid grid-cols-[minmax(0,1fr)_auto_auto] gap-3 border-t border-[#e7e7e9] px-3 py-2 font-mono text-[11px]"
              >
                <span className="truncate">{execution.tool}</span>
                <span className="tabular-nums">{execution.dur_s.toFixed(2)}s</span>
                <span className={execution.ok ? "text-emerald-700" : "text-red-700"}>
                  {execution.ok ? labels.succeeded : labels.failed}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="space-y-2">
        <h4 className="text-xs font-semibold">{labels.anomalies}</h4>
        {row.anomalies.length === 0 ? (
          <p className="text-xs text-neutral-500">{labels.none}</p>
        ) : (
          <div className={cn(FLEX, "flex-wrap gap-1.5")}>
            {row.anomalies.map((anomaly) => (
              <span
                key={anomaly}
                className="rounded-md border border-red-200 bg-red-50 px-2 py-1 font-mono text-[10px] text-red-700"
              >
                {anomaly}
              </span>
            ))}
          </div>
        )}
      </section>
    </aside>
  );
}

export function RunTimelineChart({
  timeline,
  labels,
}: {
  timeline: RunTimelineResponse;
  labels: RunTimelineChartLabels;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [canvasWidth, setCanvasWidth] = useState(MIN_CANVAS_WIDTH);
  const [selectedRowIndex, setSelectedRowIndex] = useState<number | null>(null);
  const rail = useMemo(() => prioritizedRailEvents(timeline.events), [timeline.events]);
  const layout = useMemo(
    () =>
      buildTimelineLayout({
        width: canvasWidth,
        window: timeline.window,
        rows: timeline.rows,
        events: rail.events,
      }),
    [canvasWidth, rail.events, timeline.rows, timeline.window],
  );
  const selectedRow =
    selectedRowIndex === null ? null : (timeline.rows[selectedRowIndex] ?? null);

  useEffect(() => {
    const container = scrollRef.current;
    if (!container) return;
    const minimumWidth = selectedRow ? MIN_DETAIL_CANVAS_WIDTH : MIN_CANVAS_WIDTH;
    const updateWidth = () => {
      setCanvasWidth(Math.max(minimumWidth, Math.floor(container.getBoundingClientRect().width)));
    };
    updateWidth();
    window.addEventListener("resize", updateWidth);
    return () => window.removeEventListener("resize", updateWidth);
  }, [selectedRow]);

  if (timeline.rows.length === 0) {
    return (
      <section
        className="rounded-[10px] border border-[#e7e7e9] bg-white p-4"
        aria-label={labels.chart}
      >
        <p className="font-mono text-sm text-neutral-500">{labels.empty}</p>
      </section>
    );
  }

  return (
    <section aria-label={labels.chart} className="rounded-[10px] border border-[#e7e7e9] bg-white p-3">
      <div className={cn("grid gap-3", selectedRow ? "lg:grid-cols-[minmax(0,1fr)_320px]" : "")}>
        <div className={cn(MIN_W_0, "space-y-2")}>
          <div className={cn(FLEX, "items-center justify-between px-1 text-[10px] font-medium uppercase tracking-wide text-neutral-500")}>
            <span>{labels.time}</span>
            <span>{labels.eventRail}</span>
          </div>
          <div ref={scrollRef} data-testid="run-timeline-scroll" className="overflow-x-auto">
            <div
              role="group"
              aria-label={labels.visualization}
              className={cn("relative", selectedRow ? "min-w-[320px]" : "min-w-[1000px]")}
              style={{ width: `${layout.width}px`, height: `${layout.height}px` }}
            >
              <svg
                data-testid="run-timeline-geometry"
                aria-hidden="true"
                width={layout.width}
                height={layout.height}
                className="absolute inset-0 block"
              >
                <line
                  x1={layout.plot.left}
                  x2={layout.plot.right}
                  y1={layout.axisY}
                  y2={layout.axisY}
                  stroke="#d4d4d8"
                />
                {layout.ticks.map((tick) => (
                  <line
                    key={tick.timestamp}
                    x1={tick.x}
                    x2={tick.x}
                    y1={layout.axisY - 4}
                    y2={layout.track.top + layout.track.height}
                    stroke="#e7e7e9"
                    strokeDasharray="2 4"
                  />
                ))}
                {layout.connectors.map((connector) => (
                  <path
                    key={connector.eventIndex}
                    data-testid="event-connector"
                    data-event-index={connector.eventIndex}
                    data-source-x={connector.source.x}
                    data-source-y={connector.source.y}
                    data-destination-x={connector.destination.x}
                    data-destination-y={connector.destination.y}
                    d={connector.path}
                    fill="none"
                    stroke="#a1a1aa"
                    strokeWidth="1"
                  />
                ))}
                {layout.events.map((event) => (
                  <g key={event.eventIndex}>
                    <circle
                      data-testid="event-source-node"
                      data-event-index={event.eventIndex}
                      cx={event.source.x}
                      cy={event.source.y}
                      r="2.5"
                      fill="#71717a"
                    />
                    <circle
                      data-testid="event-destination-node"
                      data-event-index={event.eventIndex}
                      cx={event.destination.x}
                      cy={event.destination.y}
                      r="2.5"
                      fill="#71717a"
                    />
                  </g>
                ))}
                <rect
                  x={layout.plot.left}
                  y={layout.track.top}
                  width={layout.plot.width}
                  height={layout.track.height}
                  rx="10"
                  fill="#fafafa"
                  stroke="#e7e7e9"
                />
                {layout.turns.map((turn, index) => {
                  const row = timeline.rows[turn.rowIndex];
                  const failed = rowFailed(row);
                  return (
                    <rect
                      key={turn.rowIndex}
                      data-testid="turn-block"
                      x={turn.left}
                      y={layout.track.top}
                      width={turn.width}
                      height={layout.track.height}
                      rx="8"
                      fill={
                        failed
                          ? "var(--series-5)"
                          : index % 2 === 0
                            ? "var(--series-1)"
                            : "var(--series-3)"
                      }
                      fillOpacity={failed ? 0.95 : 0.82}
                      stroke={selectedRowIndex === turn.rowIndex ? "#18181b" : "#ffffff"}
                      strokeWidth={selectedRowIndex === turn.rowIndex ? 2 : 1}
                    />
                  );
                })}
              </svg>

              {layout.ticks.map((tick, index) => {
                const left = Math.max(0, Math.min(layout.width - 72, tick.x - 36));
                return (
                  <span
                    key={tick.timestamp}
                    data-timeline-tick=""
                    data-testid="fixed-timeline-text"
                    className={cn(
                      "absolute w-[72px] font-mono text-[10px] tabular-nums text-neutral-500",
                      index === 0 ? "text-left" : index === layout.ticks.length - 1 ? "text-right" : "text-center",
                    )}
                    style={{ left: `${Math.round(left)}px`, top: "4px" }}
                  >
                    {tickLabel(tick.timestamp)}
                  </span>
                );
              })}

              {layout.events.map((eventLayout) => {
                const event = rail.events[eventLayout.eventIndex];
                return (
                  <span
                    key={`${event.kind}-${event.ts}-${eventLayout.eventIndex}`}
                    data-testid="event-chip"
                    className={cn(
                      "absolute truncate rounded-md border px-1.5 py-0.5 text-center font-mono text-[10px] leading-4",
                      eventChipClass(event.kind),
                    )}
                    style={{
                      left: `${eventLayout.chipLeft}px`,
                      top: `${eventLayout.chipTop}px`,
                      width: `${eventLayout.chipWidth}px`,
                    }}
                    title={event.label ?? event.ts}
                  >
                    {event.kind}
                  </span>
                );
              })}

              {layout.turns.map((turn) => {
                const row = timeline.rows[turn.rowIndex];
                const label = rowLabel(row, labels);
                if (row.turn === null) {
                  return (
                    <div
                      key={turn.rowIndex}
                      aria-label={label}
                      className="absolute"
                      style={{
                        left: `${turn.left}px`,
                        top: `${layout.track.top}px`,
                        width: `${turn.width}px`,
                        height: `${layout.track.height}px`,
                      }}
                    />
                  );
                }
                return (
                  <button
                    key={turn.rowIndex}
                    type="button"
                    aria-label={label}
                    onClick={() => setSelectedRowIndex(turn.rowIndex)}
                    className="absolute rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-neutral-900 focus-visible:ring-offset-2"
                    style={{
                      left: `${turn.left}px`,
                      top: `${layout.track.top}px`,
                      width: `${turn.width}px`,
                      height: `${layout.track.height}px`,
                    }}
                  >
                    {turn.width >= 32 ? (
                      <span
                        data-testid="fixed-timeline-text"
                        className="block truncate px-1 font-mono text-[10px] font-semibold text-white"
                        style={{ left: `${turn.left}px` }}
                      >
                        {row.turn}
                      </span>
                    ) : null}
                  </button>
                );
              })}
            </div>
          </div>
          {rail.skippedCount > 0 ? (
            <p className="px-1 font-mono text-[10px] text-neutral-500">
              {labels.moreEvents(rail.skippedCount, rail.skippedSummary)}
            </p>
          ) : null}
        </div>
        {selectedRow ? (
          <TurnDetailPanel row={selectedRow} labels={labels} onClose={() => setSelectedRowIndex(null)} />
        ) : null}
      </div>
    </section>
  );
}
