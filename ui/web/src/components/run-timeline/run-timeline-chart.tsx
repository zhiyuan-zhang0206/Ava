"use client";

import {
  type FocusEvent,
  type PointerEvent as ReactPointerEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { FLEX, MIN_W_0 } from "@/lib/layout";
import type { RunTimelineResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

import { TimelineCrumbs, type TimelineCrumbEntry } from "./run-timeline-crumbs";
import {
  LayerDetailPanel,
  rowFailed,
  rowLabel,
  tickLabel,
  TimelinePopover,
  TIMELINE_POPOVER_ID,
  TurnDetailPanel,
  type RunTimelineChartLabels,
  type TimelinePopoverTarget,
} from "./run-timeline-details";
import {
  LayerTrackButtons,
  LayerTrackGeometry,
  PendingTrackButtons,
  PendingTrackGeometry,
  RawSummaryBand,
} from "./run-timeline-layers";
import { buildReadoutText, TimelineReadout } from "./run-timeline-readout";
import { panWindow, zoomWindowAround, type TimelineWindowOverride } from "./request-level";
import { buildTimelineLayout } from "./timeline-layout";

export type { RunTimelineChartLabels } from "./run-timeline-details";

const MIN_CANVAS_WIDTH = 1000;
export const MIN_DETAIL_CANVAS_WIDTH = 320;
// KEEP (task #3696 exception inventory): rail density cap — priority kinds
// first, then the rest, capped at 120 chips; the skipped remainder is
// summarized (`skippedByKind`), not drawn.
const EVENT_RAIL_LIMIT = 120;
const TIMELINE_POPOVER_WIDTH = 288;
// P4-1 (#4023) interaction detail, not a user setting: a drag shorter than
// this stays a click on the block under the pointer (the demo used the same
// threshold); only longer movements turn the gesture into a pan.
const DRAG_THRESHOLD_PX = 4;
// Rail kinds the backend still emits after the task #2591 narrowing: execution
// and halt events no longer appear on the rail (they live in turn rows).
const EVENT_RAIL_PRIORITY = new Set([
  "compact",
  "auto_compact",
  "restart_completed",
  "resurrect",
  "agent_terminated",
  "terminate",
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
  showSummaries = true,
  widthOverride,
  onDetailOpenChange,
  flipLayers,
  trail,
  onCrumbSelect,
  onFocusWindow,
  withReadout,
}: {
  timeline: RunTimelineResponse;
  labels: RunTimelineChartLabels;
  onDrillBucket: (row: RunTimelineResponse["rows"][number]) => void;
  onZoomWindow: (window: TimelineWindowOverride) => void;
  showSummaries?: boolean;
  /** Compare view only: one shared canvas width across every lane (task
   *  #3802), so the stacked time axes stay locked together. When set, the
   *  chart stops self-measuring and drops its own min-width floor. */
  widthOverride?: number;
  /** Reports detail-panel visibility; the compare view sizes every lane from
   *  whether ANY lane's panel is open. */
  onDetailOpenChange?: (open: boolean) => void;
  /** P4-1 (#4023): render the layer stack fine-first (demo flip). */
  flipLayers?: boolean;
  /** P4-1 (#4023): focus path the crumb bar shows; the compare view passes
   *  none, so it renders no crumb bar. */
  trail?: TimelineCrumbEntry[];
  /** P4-1: select a crumb (-1 = the initial window). */
  onCrumbSelect?: (index: number) => void;
  /** P4-1: double-click routes through the page so it can push a crumb
   *  (the compare view keeps the plain zoom). */
  onFocusWindow?: (window: TimelineWindowOverride, label: string) => void;
  /** P4-1: persistent hover readout above the chart. */
  withReadout?: boolean;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const visualizationRef = useRef<HTMLDivElement>(null);
  const popoverLayerRef = useRef<HTMLDivElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const [measuredWidth, setMeasuredWidth] = useState(MIN_CANVAS_WIDTH);
  // The compare view passes one width for every lane (task #3802) so the time
  // axes stay locked together; the single view keeps self-measuring.
  const canvasWidth =
    widthOverride === undefined
      ? measuredWidth
      : Math.max(MIN_DETAIL_CANVAS_WIDTH, Math.round(widthOverride));
  const [selectedRowIndex, setSelectedRowIndex] = useState<number | null>(null);
  const [selectedLayerIndex, setSelectedLayerIndex] = useState<number | null>(null);
  const [summaryOpen, setSummaryOpen] = useState(false);
  const [popoverTarget, setPopoverTarget] = useState<TimelinePopoverTarget | null>(null);
  const [hoveredLayerIndex, setHoveredLayerIndex] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  // Wheel/drag gestures batch into frames; the flush reads the latest
  // committed window so successive frames pan from the newest window (P4-1).
  const latestWindowRef = useRef(timeline.window);
  const suppressClickRef = useRef(false);
  const rail = useMemo(() => prioritizedRailEvents(timeline.events), [timeline.events]);
  const layers = showSummaries ? timeline.layers : undefined;
  const pendingSpans = showSummaries ? timeline.pending : undefined;
  const layout = useMemo(
    () =>
      buildTimelineLayout({
        width: canvasWidth,
        window: timeline.window,
        rows: timeline.rows,
        events: rail.events,
        layers,
        pending: pendingSpans,
        flipLayers,
      }),
    [canvasWidth, flipLayers, layers, pendingSpans, rail.events, timeline.rows, timeline.window],
  );
  const selectedRow =
    selectedRowIndex === null ? null : (timeline.rows[selectedRowIndex] ?? null);
  const selectedLayer =
    selectedLayerIndex === null ? null : (timeline.layers?.[selectedLayerIndex] ?? null);
  const tickSpacingMs =
    (Date.parse(timeline.window.to) - Date.parse(timeline.window.from)) /
    (layout.ticks.length - 1);
  const includeTickSeconds = tickSpacingMs < 60_000;

  const readPopoverKey = (
    element: HTMLButtonElement,
  ): Pick<TimelinePopoverTarget, "kind" | "index"> => {
    const kind = element.dataset.timelinePopoverKind;
    if (kind !== "turn" && kind !== "event" && kind !== "pending") {
      throw new Error("Timeline popover trigger is missing its target kind");
    }
    const index = Number(element.dataset.timelinePopoverIndex);
    if (!Number.isInteger(index)) {
      throw new Error("Timeline popover trigger is missing its target index");
    }
    return { kind, index };
  };

  const showPopoverFor = (element: HTMLButtonElement) => {
    const layer = popoverLayerRef.current;
    if (!layer) return;
    const { kind, index } = readPopoverKey(element);
    const targetBox = element.getBoundingClientRect();
    const layerBox = layer.getBoundingClientRect();
    const layerWidth = layerBox.width || canvasWidth;
    const width = Math.min(TIMELINE_POPOVER_WIDTH, Math.max(0, layerWidth - 16));
    const targetLeft = targetBox.width
      ? targetBox.left - layerBox.left
      : Number.parseFloat(element.style.left) || 0;
    const targetTop = targetBox.height
      ? targetBox.bottom - layerBox.top
      : (Number.parseFloat(element.style.top) || 0) +
        (Number.parseFloat(element.style.height) || 0);
    const centeredLeft = targetLeft + targetBox.width / 2 - width / 2;
    const left = Math.max(8, Math.min(layerWidth - width - 8, centeredLeft));
    setPopoverTarget({ kind, index, left, top: targetTop, width });
  };

  const showPopover = (
    event: ReactPointerEvent<HTMLButtonElement> | FocusEvent<HTMLButtonElement>,
  ) => {
    showPopoverFor(event.currentTarget);
  };

  const hidePopover = (
    event: ReactPointerEvent<HTMLButtonElement> | FocusEvent<HTMLButtonElement>,
  ) => {
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
  const hoveredPending = popoverTarget?.kind === "pending";
  const hoveredLayer =
    hoveredLayerIndex === null ? null : (timeline.layers?.[hoveredLayerIndex] ?? null);
  const hoveredPendingBlock =
    popoverTarget?.kind === "pending" ? (layout.pendingBlocks[popoverTarget.index] ?? null) : null;
  const readoutText = buildReadoutText(
    {
      row: hoveredRow,
      event: hoveredEvent,
      layer: hoveredLayer,
      pending: hoveredPendingBlock
        ? { start: hoveredPendingBlock.start, end: hoveredPendingBlock.end }
        : null,
    },
    labels,
  );
  const focusWindow = (window: TimelineWindowOverride, label: string) => {
    if (onFocusWindow) {
      onFocusWindow(window, label);
    } else {
      onZoomWindow(window);
    }
  };

  useEffect(() => {
    latestWindowRef.current = timeline.window;
  }, [timeline.window]);

  useEffect(() => {
    if (widthOverride !== undefined) return;
    const container = scrollRef.current;
    if (!container) return;
    const minimumWidth = selectedRow ? MIN_DETAIL_CANVAS_WIDTH : MIN_CANVAS_WIDTH;
    const updateWidth = () => {
      setMeasuredWidth(Math.max(minimumWidth, Math.floor(container.getBoundingClientRect().width)));
    };
    updateWidth();
    window.addEventListener("resize", updateWidth);
    return () => window.removeEventListener("resize", updateWidth);
  }, [selectedRow, widthOverride]);

  // Detail-panel visibility is a shared concern in the compare view: it sizes
  // every lane from whether ANY lane's panel is open.
  useEffect(() => {
    onDetailOpenChange?.(selectedRow !== null || selectedLayer !== null);
    return () => onDetailOpenChange?.(false);
  }, [onDetailOpenChange, selectedRow, selectedLayer]);

  useEffect(() => {
    const visualization = visualizationRef.current;
    if (!visualization) return;
    // P4-1 (#4023, demo parity): plain wheel pans the window — accumulated
    // per animation frame so at most one window update lands per frame —
    // while Ctrl/⌘+wheel zooms around the cursor (exp sensitivity 0.0022,
    // the demo's). preventDefault stays scoped to this element, so the page
    // scroll is never hijacked outside the chart.
    let frame = 0;
    let pendingPan = 0;
    const flushPan = () => {
      frame = 0;
      const fraction = pendingPan;
      pendingPan = 0;
      if (fraction === 0) return;
      onZoomWindow(panWindow(latestWindowRef.current, fraction, new Date()));
    };
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const bounds = visualization.getBoundingClientRect();
      const cursorX = event.clientX - bounds.left;
      const anchor = Math.max(
        0,
        Math.min(1, (cursorX - layout.plot.left) / layout.plot.width),
      );
      if (event.ctrlKey || event.metaKey) {
        if (event.deltaY === 0) return;
        const factor = Math.exp(event.deltaY * 0.0022);
        onZoomWindow(zoomWindowAround(latestWindowRef.current, factor, anchor, new Date()));
        return;
      }
      const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
      pendingPan += (delta / Math.max(200, layout.plot.width)) * 1.15;
      if (frame === 0) frame = requestAnimationFrame(flushPan);
    };
    visualization.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      visualization.removeEventListener("wheel", onWheel);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [layout.plot.left, layout.plot.width, onZoomWindow]);

  useEffect(() => {
    const visualization = visualizationRef.current;
    if (!visualization) return;
    // P4-1 (#4023): drag pans the window (grab semantics: pulling left shows
    // later times). Once the gesture exceeds the threshold its click is
    // suppressed, so click-to-read stays reliable on every block.
    let drag: { startX: number; base: TimelineWindowOverride } | null = null;
    let frame = 0;
    let pendingDx = 0;
    let moved = false;
    const flushPan = () => {
      frame = 0;
      const dx = pendingDx;
      pendingDx = 0;
      if (!drag || !moved || dx === 0) return;
      onZoomWindow(panWindow(drag.base, -dx / Math.max(200, layout.plot.width), new Date()));
    };
    const onPointerDown = (event: PointerEvent) => {
      if (event.button !== 0) return;
      drag = { startX: event.clientX, base: latestWindowRef.current };
      moved = false;
      suppressClickRef.current = false;
    };
    const onPointerMove = (event: PointerEvent) => {
      if (!drag) return;
      const dx = event.clientX - drag.startX;
      if (!moved) {
        if (Math.abs(dx) <= DRAG_THRESHOLD_PX) return;
        moved = true;
        setDragging(true);
      }
      pendingDx = dx;
      if (frame === 0) frame = requestAnimationFrame(flushPan);
    };
    const endDrag = () => {
      if (!drag) return;
      if (moved) {
        suppressClickRef.current = true;
        // The click that follows pointerup lands within a frame; clear the
        // flag right after so it can never stick.
        window.setTimeout(() => {
          suppressClickRef.current = false;
        }, 150);
      }
      drag = null;
      moved = false;
      pendingDx = 0;
      setDragging(false);
      if (frame) {
        cancelAnimationFrame(frame);
        frame = 0;
      }
    };
    visualization.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", endDrag);
    window.addEventListener("pointercancel", endDrag);
    return () => {
      visualization.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", endDrag);
      window.removeEventListener("pointercancel", endDrag);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [layout.plot.width, onZoomWindow]);

  if (timeline.rows.length === 0) {
    return (
      <section
        className="rounded-[10px] border border-border bg-card p-4"
        aria-label={labels.chart}
      >
        <p className="font-mono text-sm text-muted-foreground">{labels.empty}</p>
      </section>
    );
  }

  return (
    <section aria-label={labels.chart} className="rounded-[10px] border border-border bg-card p-3">
      <div className={cn("grid gap-3", selectedRow || selectedLayer ? "lg:grid-cols-[minmax(0,1fr)_320px]" : "")}>
        <div ref={popoverLayerRef} className={cn(MIN_W_0, "relative space-y-2")}>
          {showSummaries && timeline.summary ? (
            <RawSummaryBand
              summary={timeline.summary}
              labels={labels}
              open={summaryOpen}
              onToggle={() => setSummaryOpen((open) => !open)}
            />
          ) : null}
          <div className={cn(FLEX, "items-center justify-between px-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground")}>
            <span>{labels.time}</span>
            <span>{labels.eventRail}</span>
          </div>
          {trail !== undefined ? (
            <TimelineCrumbs entries={trail} labels={labels} onSelect={onCrumbSelect} />
          ) : null}
          {withReadout ? <TimelineReadout text={readoutText} labels={labels} /> : null}
          <div ref={scrollRef} data-testid="run-timeline-scroll" className="overflow-x-auto">
            <div
              ref={visualizationRef}
              data-testid="run-timeline-visualization"
              role="group"
              aria-label={labels.visualization}
              onClickCapture={(event) => {
                if (suppressClickRef.current) {
                  event.preventDefault();
                  event.stopPropagation();
                  suppressClickRef.current = false;
                }
              }}
              className={cn(
                "relative",
                dragging && "cursor-grabbing select-none",
                widthOverride === undefined &&
                  (selectedRow ? "min-w-[320px]" : "min-w-[1000px]"),
              )}
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
                {layout.layerRows.length > 0 && timeline.layers ? (
                  <LayerTrackGeometry rows={layout.layerRows} selectedIndex={selectedLayerIndex} />
                ) : null}
                {layout.pendingBlocks.length > 0 ? (
                  <PendingTrackGeometry row={layout.pendingRow} blocks={layout.pendingBlocks} />
                ) : null}
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
                      "absolute w-[72px] font-mono text-[10px] tabular-nums text-muted-foreground",
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
                      "absolute truncate rounded-md border px-1.5 py-0.5 text-center font-mono text-[10px] leading-4 outline-none focus-visible:ring-2 focus-visible:ring-foreground focus-visible:ring-offset-2",
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
                        setSelectedLayerIndex(null);
                        setSummaryOpen(false);
                      }
                      // The panel supersedes the hover card: clicking a block
                      // must not leave the popover covering the track (the
                      // pointer never left the block).
                      setPopoverTarget(null);
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
                        className="block truncate px-1 font-mono text-[10px] font-semibold text-white"
                        style={{ left: `${turn.left}px` }}
                      >
                        {row.turn}
                      </span>
                    ) : null}
                  </button>
                );
              })}

              {layout.layerRows.length > 0 && timeline.layers ? (
                <LayerTrackButtons
                  rows={layout.layerRows}
                  layers={timeline.layers}
                  labels={labels}
                  onSelect={(index) => {
                    setSelectedLayerIndex(index);
                    setSelectedRowIndex(null);
                    setSummaryOpen(false);
                  }}
                  onZoom={focusWindow}
                  onHover={withReadout ? setHoveredLayerIndex : undefined}
                />
              ) : null}

              {layout.pendingBlocks.length > 0 ? (
                <PendingTrackButtons
                  row={layout.pendingRow}
                  blocks={layout.pendingBlocks}
                  labels={labels}
                  onShowPopover={showPopoverFor}
                  onHidePopover={hidePopover}
                  onZoom={focusWindow}
                  describedIndex={popoverTarget?.kind === "pending" ? popoverTarget.index : null}
                />
              ) : null}
            </div>
          </div>
          {popoverTarget && (hoveredRow || hoveredEvent || hoveredPending) ? (
            <TimelinePopover
              target={popoverTarget}
              row={hoveredRow}
              event={hoveredEvent}
              pending={hoveredPending}
              labels={labels}
              popoverRef={popoverRef}
              onPointerLeave={() => setPopoverTarget(null)}
            />
          ) : null}
          {rail.skippedCount > 0 ? (
            <p className="px-1 font-mono text-[10px] text-muted-foreground">
              {labels.moreEvents(rail.skippedCount, rail.skippedSummary)}
            </p>
          ) : null}
        </div>
        {selectedRow ? (
          <TurnDetailPanel row={selectedRow} labels={labels} onClose={() => setSelectedRowIndex(null)} />
        ) : selectedLayer ? (
          <LayerDetailPanel node={selectedLayer} labels={labels} onClose={() => setSelectedLayerIndex(null)} />
        ) : null}
      </div>
    </section>
  );
}
