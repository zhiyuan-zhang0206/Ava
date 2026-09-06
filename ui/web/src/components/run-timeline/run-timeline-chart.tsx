"use client";

import {
  type FocusEvent,
  type PointerEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { FLEX, MIN_W_0 } from "@/lib/layout";
import type { RunTimelineResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

import {
  rowFailed,
  rowLabel,
  tickLabel,
  TimelinePopover,
  TIMELINE_POPOVER_ID,
  TurnDetailPanel,
  type RunTimelineChartLabels,
  type TimelinePopoverTarget,
} from "./run-timeline-details";
import { zoomWindowAround, type TimelineWindowOverride } from "./request-level";
import { buildTimelineLayout } from "./timeline-layout";

export type { RunTimelineChartLabels } from "./run-timeline-details";

const MIN_CANVAS_WIDTH = 1000;
const MIN_DETAIL_CANVAS_WIDTH = 320;
const EVENT_RAIL_LIMIT = 120;
const TIMELINE_POPOVER_WIDTH = 288;
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

function eventChipClass(kind: string): string {
  if (kind.includes("failed") || kind.includes("timeout")) {
    return "border-[var(--series-5)] bg-red-50 text-red-700 dark:bg-red-950/30 dark:text-red-400";
  }
  if (kind === "compact" || kind === "auto_compact") {
    return "border-violet-300 bg-violet-50 text-violet-700 dark:bg-violet-950/30 dark:text-violet-400";
  }
  if (kind.includes("restart") || kind.includes("resurrect")) {
    return "border-blue-300 bg-blue-50 text-blue-700 dark:bg-blue-950/30 dark:text-blue-400";
  }
  return "border-border bg-card text-muted-foreground";
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

export function RunTimelineChart({
  timeline,
  labels,
  onDrillBucket,
  onZoomWindow,
}: {
  timeline: RunTimelineResponse;
  labels: RunTimelineChartLabels;
  onDrillBucket: (row: RunTimelineResponse["rows"][number]) => void;
  onZoomWindow: (window: TimelineWindowOverride) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const visualizationRef = useRef<HTMLDivElement>(null);
  const popoverLayerRef = useRef<HTMLDivElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const [canvasWidth, setCanvasWidth] = useState(MIN_CANVAS_WIDTH);
  const [selectedRowIndex, setSelectedRowIndex] = useState<number | null>(null);
  const [popoverTarget, setPopoverTarget] = useState<TimelinePopoverTarget | null>(null);
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
  const tickSpacingMs =
    (Date.parse(timeline.window.to) - Date.parse(timeline.window.from)) /
    (layout.ticks.length - 1);
  const includeTickSeconds = tickSpacingMs < 60_000;

  const readPopoverKey = (
    element: HTMLButtonElement,
  ): Pick<TimelinePopoverTarget, "kind" | "index"> => {
    const kind = element.dataset.timelinePopoverKind;
    if (kind !== "turn" && kind !== "event") {
      throw new Error("Timeline popover trigger is missing its target kind");
    }
    const index = Number(element.dataset.timelinePopoverIndex);
    if (!Number.isInteger(index)) {
      throw new Error("Timeline popover trigger is missing its target index");
    }
    return { kind, index };
  };

  const showPopover = (event: PointerEvent<HTMLButtonElement> | FocusEvent<HTMLButtonElement>) => {
    const layer = popoverLayerRef.current;
    if (!layer) return;
    const { kind, index } = readPopoverKey(event.currentTarget);
    const targetBox = event.currentTarget.getBoundingClientRect();
    const layerBox = layer.getBoundingClientRect();
    const layerWidth = layerBox.width || canvasWidth;
    const width = Math.min(TIMELINE_POPOVER_WIDTH, Math.max(0, layerWidth - 16));
    const targetLeft = targetBox.width
      ? targetBox.left - layerBox.left
      : Number.parseFloat(event.currentTarget.style.left) || 0;
    const targetTop = targetBox.height
      ? targetBox.bottom - layerBox.top
      : (Number.parseFloat(event.currentTarget.style.top) || 0) +
        (Number.parseFloat(event.currentTarget.style.height) || 0);
    const centeredLeft = targetLeft + targetBox.width / 2 - width / 2;
    const left = Math.max(8, Math.min(layerWidth - width - 8, centeredLeft));
    setPopoverTarget({ kind, index, left, top: targetTop, width });
  };

  const hidePopover = (event: PointerEvent<HTMLButtonElement> | FocusEvent<HTMLButtonElement>) => {
    if (event.type === "pointerleave" && event.currentTarget === document.activeElement) return;
    if (event.relatedTarget instanceof Node && popoverRef.current?.contains(event.relatedTarget)) return;
    const { kind, index } = readPopoverKey(event.currentTarget);
    setPopoverTarget((current) =>
      current?.kind === kind && current.index === index ? null : current,
    );
  };

  const hoveredRow =
    popoverTarget?.kind === "turn" ? (timeline.rows[popoverTarget.index] ?? null) : null;
  const hoveredEvent =
    popoverTarget?.kind === "event" ? (rail.events[popoverTarget.index] ?? null) : null;

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

  useEffect(() => {
    const visualization = visualizationRef.current;
    if (!visualization) return;
    const zoomOnWheel = (event: WheelEvent) => {
      if ((!event.ctrlKey && !event.metaKey) || event.deltaY === 0) return;
      event.preventDefault();
      const bounds = visualization.getBoundingClientRect();
      const cursorX = event.clientX - bounds.left;
      const anchor = Math.max(
        0,
        Math.min(1, (cursorX - layout.plot.left) / layout.plot.width),
      );
      const factor = event.deltaY < 0 ? 0.8 : 1.25;
      onZoomWindow(zoomWindowAround(timeline.window, factor, anchor, new Date()));
    };
    visualization.addEventListener("wheel", zoomOnWheel, { passive: false });
    return () => visualization.removeEventListener("wheel", zoomOnWheel);
  }, [layout.plot.left, layout.plot.width, onZoomWindow, timeline.window]);

  if (timeline.rows.length === 0) {
    return (
      <section
        className="rounded-[10px] border border-border bg-card p-4"
        aria-label={labels.chart}
      >
        <p className="font-sans text-sm text-muted-foreground">{labels.empty}</p>
      </section>
    );
  }

  return (
    <section aria-label={labels.chart} className="rounded-[10px] border border-border bg-card p-3">
      <div className={cn("grid gap-3", selectedRow ? "lg:grid-cols-[minmax(0,1fr)_320px]" : "")}>
        <div ref={popoverLayerRef} className={cn(MIN_W_0, "relative space-y-2")}>
          <div className={cn(FLEX, "items-center justify-between px-1 text-2xs font-medium uppercase tracking-wide text-muted-foreground")}>
            <span>{labels.time}</span>
            <span>{labels.eventRail}</span>
          </div>
          <div ref={scrollRef} data-testid="run-timeline-scroll" className="overflow-x-auto">
            <div
              ref={visualizationRef}
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
                  stroke="var(--border)"
                />
                {layout.ticks.map((tick) => (
                  <line
                    key={tick.timestamp}
                    x1={tick.x}
                    x2={tick.x}
                    y1={layout.axisY - 4}
                    y2={layout.track.top + layout.track.height}
                    stroke="var(--border)"
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
                  fill="var(--muted)"
                  stroke="var(--border)"
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
                      stroke={selectedRowIndex === turn.rowIndex ? "var(--foreground)" : "var(--card)"}
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
                      "absolute w-[72px] font-mono text-2xs tabular-nums text-muted-foreground",
                      index === 0 ? "text-left" : index === layout.ticks.length - 1 ? "text-right" : "text-center",
                    )}
                    style={{ left: `${Math.round(left)}px`, top: "4px" }}
                  >
                    {tickLabel(tick.timestamp, includeTickSeconds)}
                  </span>
                );
              })}

              {layout.events.map((eventLayout) => {
                const event = rail.events[eventLayout.eventIndex];
                return (
                  <button
                    key={`${event.kind}-${event.ts}-${eventLayout.eventIndex}`}
                    type="button"
                    data-testid="event-chip"
                    data-timeline-popover-kind="event"
                    data-timeline-popover-index={eventLayout.eventIndex}
                    aria-describedby={
                      popoverTarget?.kind === "event" && popoverTarget.index === eventLayout.eventIndex
                        ? TIMELINE_POPOVER_ID
                        : undefined
                    }
                    onPointerEnter={showPopover}
                    onPointerLeave={hidePopover}
                    onFocus={showPopover}
                    onBlur={hidePopover}
                    className={cn(
                      "absolute truncate rounded-md border px-1.5 py-0.5 text-center font-mono text-2xs leading-4 outline-none focus-visible:ring-2 focus-visible:ring-foreground focus-visible:ring-offset-2",
                      eventChipClass(event.kind),
                    )}
                    style={{
                      left: `${eventLayout.chipLeft}px`,
                      top: `${eventLayout.chipTop}px`,
                      width: `${eventLayout.chipWidth}px`,
                    }}
                  >
                    {event.kind}
                  </button>
                );
              })}

              {layout.turns.map((turn) => {
                const row = timeline.rows[turn.rowIndex];
                const label = rowLabel(row, labels);
                return (
                  <button
                    key={turn.rowIndex}
                    type="button"
                    aria-label={label}
                    aria-describedby={
                      popoverTarget?.kind === "turn" && popoverTarget.index === turn.rowIndex
                        ? TIMELINE_POPOVER_ID
                        : undefined
                    }
                    data-timeline-popover-kind="turn"
                    data-timeline-popover-index={turn.rowIndex}
                    onPointerEnter={showPopover}
                    onPointerLeave={hidePopover}
                    onFocus={showPopover}
                    onBlur={hidePopover}
                    onClick={() => {
                      if (row.turn === null) {
                        onDrillBucket(row);
                      } else {
                        setSelectedRowIndex(turn.rowIndex);
                      }
                    }}
                    className="absolute rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-foreground focus-visible:ring-offset-2"
                    style={{
                      left: `${turn.left}px`,
                      top: `${layout.track.top}px`,
                      width: `${turn.width}px`,
                      height: `${layout.track.height}px`,
                    }}
                  >
                    {row.turn !== null && turn.width >= 32 ? (
                      <span
                        data-testid="fixed-timeline-text"
                        className="block truncate px-1 font-mono text-2xs font-semibold text-white"
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
          {popoverTarget && (hoveredRow || hoveredEvent) ? (
            <TimelinePopover
              target={popoverTarget}
              row={hoveredRow}
              event={hoveredEvent}
              labels={labels}
              popoverRef={popoverRef}
              onPointerLeave={() => setPopoverTarget(null)}
            />
          ) : null}
          {rail.skippedCount > 0 ? (
            <p className="px-1 font-mono text-2xs text-muted-foreground">
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
