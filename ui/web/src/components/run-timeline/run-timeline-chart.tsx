"use client";

import {
  type FocusEvent,
  type PointerEvent as ReactPointerEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { formatTokensCompact } from "@/lib/format-number";
import { FLEX, MIN_W_0 } from "@/lib/layout";
import type { RunTimelineResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

import type { TimelineContextView } from "./context-view";
import { TimelineCrumbs, type TimelineCrumbEntry } from "./run-timeline-crumbs";
import {
  LayerDetailPanel,
  layerFocusLabel,
  rowFailed,
  rowLabel,
  TimelinePopover,
  TIMELINE_POPOVER_ID,
  TurnDetailPanel,
  type RunTimelineChartLabels,
  type TimelinePopoverTarget,
} from "./run-timeline-details";
import { StripLegend } from "./run-timeline-legend";
import { MessageDetailPanel, type MessageFocusTarget } from "./run-timeline-message-panel";
import { StripTrackButtons, StripTrackGeometry, StripTruncatedHint } from "./run-timeline-strip";
import {
  LayerTrackButtons,
  LayerTrackGeometry,
  PendingTrackButtons,
  PendingTrackGeometry,
  RawSummaryBand,
} from "./run-timeline-layers";
import { buildReadoutText, TimelineReadout } from "./run-timeline-readout";
import { eventChipClass, prioritizedRailEvents } from "./run-timeline-rail";
import { useTimelineGestures } from "./use-timeline-gestures";
import type { TimelineWindowOverride } from "./request-level";
import { coveredMessageIndexes, messageChainIndexes } from "./strip-layout";
import type { StripLegendCategory } from "./strip-categories";
import { buildTimelineLayout, clampIntervalToPlot } from "./timeline-layout";

export type { RunTimelineChartLabels } from "./run-timeline-details";

const MIN_CANVAS_WIDTH = 1000;
export const MIN_DETAIL_CANVAS_WIDTH = 320;
const TIMELINE_POPOVER_WIDTH = 288;
// P4-2 (#4023): a stable empty set for the strip relationship memos, so the
// consumers do not see a fresh identity on every render.
const NO_INDEXES: ReadonlySet<number> = new Set();
export function RunTimelineChart({
  timeline,
  labels,
  onDrillBucket,
  onZoomWindow,
  showSummaries = true,
  widthOverride,
  minHeight,
  onDetailOpenChange,
  flipLayers,
  trail,
  onCrumbSelect,
  onFocusWindow,
  withReadout,
  showStrip,
  activeCategory: controlledCategory,
  onActiveCategoryChange,
  showLegend,
  onHoverMessage,
  axis = "time",
  contextView,
  contextTotal,
  onContextView,
  onContextFocus,
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
  /** Single-run pages reserve viewport space; compare lanes keep their content height. */
  minHeight?: string;
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
  /** P4-2b (#4023): the x projection — the page's axis toggle drives it; the
   *  compare view never passes it (stays on the time axis). */
  axis?: "time" | "context";
  /** P4-2b: char-domain viewport + domain size of the context axis (present
   *  whenever `axis` is "context"). */
  contextView?: TimelineContextView;
  contextTotal?: number;
  /** P4-2b: gestures and +/- route here on the context axis — pure viewport
   *  updates that never refetch. */
  onContextView?: (view: TimelineContextView) => void;
  /** P4-2b: double-click focus on the context axis (char-range crumb push,
   *  the mirror of onFocusWindow). */
  onContextFocus?: (view: TimelineContextView, label: string) => void;
  /** P4-1: persistent hover readout above the chart. */
  withReadout?: boolean;
  /** P4-4 (#4023): strip opt-in — the compare view renders its per-lane strip
   *  despite the shared `widthOverride` canvas; unset keeps the single-view
   *  default (strip iff the response carries messages). */
  showStrip?: boolean;
  /** P4-4: controlled legend selection — one shared legend row drives every
   *  compare lane; unset keeps the chart's internal state. */
  activeCategory?: StripLegendCategory | null;
  /** P4-4: legend toggled — fires in both the controlled and internal modes. */
  onActiveCategoryChange?: (category: StripLegendCategory | null) => void;
  /** P4-4: render the legend row (default true); the compare view hides the
   *  per-lane legends and renders one shared row instead. */
  showLegend?: boolean;
  /** P4-4: hovered strip message index (null on leave) — the compare view
   *  builds its single readout line from it. */
  onHoverMessage?: (index: number | null) => void;
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
  const [selectedMessageIndex, setSelectedMessageIndex] = useState<number | null>(null);
  const [summaryOpen, setSummaryOpen] = useState(false);
  const [popoverTarget, setPopoverTarget] = useState<TimelinePopoverTarget | null>(null);
  const [hoveredLayerIndex, setHoveredLayerIndex] = useState<number | null>(null);
  const [hoveredMessageIndex, setHoveredMessageIndex] = useState<number | null>(null);
  const [internalCategory, setInternalCategory] = useState<StripLegendCategory | null>(null);
  // P4-4 (#4023): controlled when the caller passes `activeCategory` (the
  // compare view owns one legend for every lane); unset keeps the internal
  // state of the single view. `onActiveCategoryChange` fires either way.
  const activeCategory = controlledCategory !== undefined ? controlledCategory : internalCategory;
  const toggleCategory = (category: StripLegendCategory) => {
    const next = activeCategory === category ? null : category;
    if (controlledCategory === undefined) setInternalCategory(next);
    onActiveCategoryChange?.(next);
  };
  const [dragging, setDragging] = useState(false);
  // P4-1 (#4023): the pan/zoom gesture wiring lives in useTimelineGestures
  // (refs + unconditional finalization, see the PR #2887 review); the chart
  // owns only the click suppression the drag sets.
  const suppressClickRef = useRef(false);
  const rail = useMemo(() => prioritizedRailEvents(timeline.events), [timeline.events]);
  const layers = showSummaries ? timeline.layers : undefined;
  const pendingSpans = showSummaries ? timeline.pending : undefined;
  // P4-2 (#4023): the strip renders when the response carries the messages
  // field (null = degraded read, same stance as layers/inbounds). P4-4: the
  // compare view opts in explicitly (`showStrip`) under its shared
  // `widthOverride` canvas; unset keeps the single view's default.
  const stripAllowed = showStrip ?? (widthOverride === undefined);
  const stripMessages = stripAllowed ? (timeline.messages ?? undefined) : undefined;
  const layout = useMemo(
    () =>
      buildTimelineLayout({
        width: canvasWidth,
        window: timeline.window,
        rows: timeline.rows,
        events: rail.events,
        layers,
        pending: pendingSpans,
        messages: stripMessages,
        flipLayers,
        axis,
        contextView,
      }),
    [
      axis,
      canvasWidth,
      contextView,
      flipLayers,
      layers,
      pendingSpans,
      rail.events,
      stripMessages,
      timeline.rows,
      timeline.window,
    ],
  );
  const contextMode = axis === "context";
  // P4-2b: cumulative char offsets of the strip messages — one source for
  // the context focus targets and readout positions.
  const messageOffsets = useMemo(() => {
    const offsets = [0];
    for (const message of timeline.messages ?? []) {
      offsets.push(offsets[offsets.length - 1] + Math.max(0, message.chars));
    }
    return offsets;
  }, [timeline.messages]);
  const selectedRow =
    selectedRowIndex === null ? null : (timeline.rows[selectedRowIndex] ?? null);
  const selectedLayer =
    selectedLayerIndex === null ? null : (timeline.layers?.[selectedLayerIndex] ?? null);
  const selectedMessage =
    selectedMessageIndex === null ? null : (timeline.messages?.[selectedMessageIndex] ?? null);
  // P4-2 (#4023) cross-highlights: selecting a summary node lights its
  // covered messages ("related", the demo's rule); selecting a message
  // outlines the chain of summary blocks covering it ("path").
  const relatedMessageIndexes = useMemo(() => {
    if (selectedLayerIndex === null || !timeline.layers || !timeline.messages) return NO_INDEXES;
    const node = timeline.layers[selectedLayerIndex];
    return new Set(coveredMessageIndexes(timeline.messages, node.start, node.end));
  }, [selectedLayerIndex, timeline.layers, timeline.messages]);
  const messageChain = useMemo(() => {
    if (selectedMessageIndex === null || !timeline.messages) return [];
    return messageChainIndexes(timeline.layers, timeline.messages[selectedMessageIndex]?.ts ?? null);
  }, [selectedMessageIndex, timeline.messages, timeline.layers]);
  const chainLayerIndexes = useMemo(() => new Set(messageChain), [messageChain]);

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
  const hoveredMessage =
    hoveredMessageIndex === null ? null : (timeline.messages?.[hoveredMessageIndex] ?? null);
  const hoveredMessageChain =
    hoveredMessage === null ? [] : messageChainIndexes(timeline.layers, hoveredMessage.ts);
  const hoveredMessageLeaf =
    hoveredMessageChain.length > 0
      ? (timeline.layers?.[hoveredMessageChain[hoveredMessageChain.length - 1]] ?? null)
      : null;
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
      message: hoveredMessage
        ? {
            message: hoveredMessage,
            leaf: hoveredMessageLeaf ? layerFocusLabel(hoveredMessageLeaf) : null,
            position:
              contextMode && hoveredMessageIndex !== null
                ? `${formatTokensCompact(messageOffsets[hoveredMessageIndex])}-${formatTokensCompact(messageOffsets[hoveredMessageIndex + 1])} ${labels.charsUnit}`
                : undefined,
          }
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
  /** P4-2 (#4023): the time window covering a strip bar plus padding — the
   *  demo's "zoom to this message". Null when the strip is absent or the
   *  index is out of range. */
  const messageFocusWindow = (index: number): TimelineWindowOverride | null => {
    const strip = layout.strip;
    if (!strip) return null;
    const bar = strip.messages.at(index);
    if (!bar) return null;
    const spanMs = Date.parse(timeline.window.to) - Date.parse(timeline.window.from);
    const toTime = (px: number) =>
      Date.parse(timeline.window.from) + ((px - layout.plot.left) / layout.plot.width) * spanMs;
    const padPx = Math.max(0.6 * bar.width, layout.plot.width / 200);
    const fromPx = Math.max(layout.plot.left, bar.left - padPx);
    const toPx = Math.max(
      fromPx + 0.5,
      Math.min(layout.plot.left + layout.plot.width, bar.left + bar.width + padPx),
    );
    return {
      from: new Date(toTime(fromPx)).toISOString(),
      to: new Date(toTime(toPx)).toISOString(),
    };
  };
  /** P4-2b (#4023): a char range padded by half its width, floored at a
   *  two-hundredth of the domain — the shared focus padding of the strip
   *  bars and the layer blocks (the demo's rule). */
  const contextFocusRange = (from: number, to: number): TimelineContextView | null => {
    if (contextTotal === undefined) return null;
    const pad = Math.max(0.5 * (to - from), contextTotal / 200);
    return { from: from - pad, to: to + pad };
  };
  /** P4-2b (#4023): the char range covering a strip message plus padding —
   *  the context axis' "zoom to this message" (mirror of messageFocusWindow). */
  const contextMessageFocus = (index: number): TimelineContextView | null => {
    if (timeline.messages?.at(index) === undefined) return null;
    return contextFocusRange(messageOffsets[index], messageOffsets[index + 1]);
  };
  /** P4-2b: the char range covering a layer block's messages plus padding —
   *  the block's "zoom to this block" on the context axis (the mirror of
   *  contextMessageFocus over the covered-message union). */
  const contextBlockFocus = (nodeIndex: number): TimelineContextView | null => {
    const node = timeline.layers?.at(nodeIndex);
    const messages = timeline.messages;
    if (node === undefined || !messages) return null;
    const covered = coveredMessageIndexes(messages, node.start, node.end);
    if (covered.length === 0) return null;
    return contextFocusRange(
      messageOffsets[covered[0]],
      messageOffsets[covered[covered.length - 1] + 1],
    );
  };
  /** P4-2b: the panel's "zoom to this message" routes through the active
   *  axis. */
  const messageFocusTarget = (index: number): MessageFocusTarget | null => {
    if (contextMode) {
      const view = contextMessageFocus(index);
      return view === null ? null : { kind: "context", view };
    }
    const window = messageFocusWindow(index);
    return window === null ? null : { kind: "time", window };
  };
  const selectedMessageFocus =
    selectedMessageIndex === null ? null : messageFocusTarget(selectedMessageIndex);
  const focusMessage = (target: MessageFocusTarget, label: string) => {
    if (target.kind === "time") {
      focusWindow(target.window, label);
    } else {
      onContextFocus?.(target.view, label);
    }
  };
  // Selection is exclusive across the three detail sources (turn / summary
  // node / raw message): each picker clears the other two and closes the
  // summary band the panel supersedes.
  const selectRow = (index: number) => {
    setSelectedRowIndex(index);
    setSelectedLayerIndex(null);
    setSelectedMessageIndex(null);
    setSummaryOpen(false);
  };
  const selectLayer = (index: number) => {
    setSelectedLayerIndex(index);
    setSelectedRowIndex(null);
    setSelectedMessageIndex(null);
    setSummaryOpen(false);
  };
  const selectMessage = (index: number) => {
    setSelectedMessageIndex(index);
    setSelectedRowIndex(null);
    setSelectedLayerIndex(null);
    setSummaryOpen(false);
  };

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
    onDetailOpenChange?.(
      selectedRow !== null || selectedLayer !== null || selectedMessage !== null,
    );
    return () => onDetailOpenChange?.(false);
  }, [onDetailOpenChange, selectedRow, selectedLayer, selectedMessage]);

  useTimelineGestures({
    visualizationRef,
    plot: layout.plot,
    mode: contextMode ? "context" : "time",
    window: timeline.window,
    zoomWindow: onZoomWindow,
    contextView,
    contextTotal,
    onContextView,
    suppressClickRef,
    setDragging,
  });

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
      <div
        className={cn(
          "grid gap-3",
          selectedRow || selectedLayer || selectedMessage ? "lg:grid-cols-[minmax(0,1fr)_320px]" : "",
        )}
      >
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
              style={{ width: `${layout.width}px`, height: `${layout.height}px`, minHeight }}
            >
              <svg
                data-testid="run-timeline-geometry"
                aria-hidden="true"
                width={layout.width}
                height={layout.height}
                className="absolute inset-0 block"
              >
                <defs>
                  <clipPath id="run-timeline-plot-clip">
                    <rect
                      x={layout.plot.left}
                      y="0"
                      width={layout.plot.width}
                      height={layout.height}
                    />
                  </clipPath>
                </defs>
                <line
                  x1={layout.plot.left}
                  x2={layout.plot.right}
                  y1={layout.axisY}
                  y2={layout.axisY}
                  stroke="var(--border)"
                />
                {layout.ticks.map((tick) => (
                  <line
                    key={tick.key}
                    x1={tick.x}
                    x2={tick.x}
                    y1={layout.axisY - 4}
                    y2={layout.track.top + layout.track.height}
                    stroke="var(--border)"
                    strokeDasharray="2 4"
                  />
                ))}
                {contextMode ? null : layout.connectors.map((connector) => (
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
                {contextMode ? null : layout.events.map((event) => (
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
                <g clipPath="url(#run-timeline-plot-clip)">
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
                  <LayerTrackGeometry
                    rows={layout.layerRows}
                    selectedIndex={selectedLayerIndex}
                    pathIndexes={chainLayerIndexes}
                    dimmed={activeCategory !== null}
                  />
                ) : null}
                {!contextMode && layout.pendingBlocks.length > 0 ? (
                  <PendingTrackGeometry row={layout.pendingRow} blocks={layout.pendingBlocks} />
                ) : null}
                {layout.strip && stripMessages ? (
                  <StripTrackGeometry
                    plot={layout.plot}
                    row={layout.strip}
                    messages={stripMessages}
                    selectedIndex={selectedMessageIndex}
                    relatedIndexes={relatedMessageIndexes}
                    activeCategory={activeCategory}
                  />
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
                </g>
              </svg>

              <div
                role="group"
                aria-label={contextMode ? labels.charTicks : labels.time}
                className="pointer-events-none absolute inset-0"
              >
                {layout.ticks.map((tick, index) => {
                  const left = Math.max(0, Math.min(layout.width - 72, tick.x - 36));
                  return (
                    <span
                      key={tick.key}
                      data-timeline-tick=""
                      data-testid="fixed-timeline-text"
                      className={cn(
                        "absolute w-[72px] font-mono text-[10px] tabular-nums text-muted-foreground",
                        index === 0 ? "text-left" : index === layout.ticks.length - 1 ? "text-right" : "text-center",
                      )}
                      style={{ left: `${Math.round(left)}px`, top: "4px" }}
                    >
                      {tick.label}
                    </span>
                  );
                })}
              </div>

              {contextMode ? null : layout.events.map((eventLayout) => {
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

              {contextMode ? (
                <p
                  data-testid="rail-hidden-hint"
                  className="absolute font-mono text-[10px] text-muted-foreground"
                  style={{ left: `${layout.plot.left}px`, top: `${layout.axisY + 8}px` }}
                >
                  {layout.pendingBlocks.length > 0 ? labels.railHiddenPending : labels.railHidden}
                </p>
              ) : null}

              {layout.turns.flatMap((turn) => {
                const row = timeline.rows[turn.rowIndex];
                const box = clampIntervalToPlot(turn.left, turn.width, layout.plot);
                if (box === null) return [];
                const label = rowLabel(row, labels);
                return [
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
                        selectRow(turn.rowIndex);
                      }
                      // The panel supersedes the hover card: clicking a block
                      // must not leave the popover covering the track (the
                      // pointer never left the block).
                      setPopoverTarget(null);
                    }}
                    className="absolute rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-foreground focus-visible:ring-offset-2"
                    style={{
                      left: `${box.left}px`,
                      top: `${layout.track.top}px`,
                      width: `${box.width}px`,
                      height: `${layout.track.height}px`,
                    }}
                  >
                    {row.turn !== null && box.width >= 32 ? (
                      <span
                        data-testid="fixed-timeline-text"
                        className="block truncate px-1 font-mono text-[10px] font-semibold text-white"
                        style={{ left: `${turn.left}px` }}
                      >
                        {row.turn}
                      </span>
                    ) : null}
                  </button>,
                ];
              })}

              {layout.layerRows.length > 0 && timeline.layers ? (
                <LayerTrackButtons
                  plot={layout.plot}
                  rows={layout.layerRows}
                  layers={timeline.layers}
                  labels={labels}
                  onSelect={selectLayer}
                  onFocus={(index) => {
                    const node = timeline.layers?.[index];
                    if (!node) return;
                    if (contextMode) {
                      const target = contextBlockFocus(index);
                      if (target && onContextFocus) {
                        onContextFocus(target, layerFocusLabel(node));
                      }
                      return;
                    }
                    focusWindow({ from: node.start, to: node.end }, layerFocusLabel(node));
                  }}
                  onHover={withReadout ? setHoveredLayerIndex : undefined}
                />
              ) : null}

              {!contextMode && layout.pendingBlocks.length > 0 ? (
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

              {layout.strip && stripMessages ? (
                <StripTrackButtons
                  plot={layout.plot}
                  row={layout.strip}
                  messages={stripMessages}
                  labels={labels}
                  onSelect={selectMessage}
                  onFocus={(index) => {
                    const message = stripMessages.at(index);
                    if (!message) return;
                    if (contextMode) {
                      const target = contextMessageFocus(index);
                      if (target && onContextFocus) {
                        onContextFocus(target, labels.messageLabel(message.idx));
                      }
                      return;
                    }
                    const target = messageFocusWindow(index);
                    if (target) {
                      focusWindow(target, labels.messageLabel(message.idx));
                    }
                  }}
                  onHover={
                    withReadout || onHoverMessage
                      ? (index) => {
                          setHoveredMessageIndex(index);
                          onHoverMessage?.(index);
                        }
                      : undefined
                  }
                />
              ) : null}
              {layout.strip && stripMessages && timeline.messages_truncated ? (
                <StripTruncatedHint plot={layout.plot} row={layout.strip} labels={labels} />
              ) : null}
            </div>
          </div>
          {showLegend !== false && layout.strip && stripMessages ? (
            <StripLegend active={activeCategory} labels={labels} onToggle={toggleCategory} />
          ) : null}
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
          {rail.skippedCount > 0 && !contextMode ? (
            <p className="px-1 font-mono text-[10px] text-muted-foreground">
              {labels.moreEvents(rail.skippedCount, rail.skippedSummary)}
            </p>
          ) : null}
        </div>
        {selectedRow ? (
          <TurnDetailPanel row={selectedRow} labels={labels} onClose={() => setSelectedRowIndex(null)} />
        ) : selectedLayer ? (
          <LayerDetailPanel node={selectedLayer} labels={labels} onClose={() => setSelectedLayerIndex(null)} />
        ) : selectedMessage ? (
          <MessageDetailPanel
            key={selectedMessage.key}
            agentId={timeline.agent_id}
            message={selectedMessage}
            chain={messageChain.flatMap((index) => {
              const node = timeline.layers?.[index];
              return node ? [{ index, node }] : [];
            })}
            labels={labels}
            focusTarget={selectedMessageFocus}
            onFocus={focusMessage}
            onClose={() => setSelectedMessageIndex(null)}
            onSelectLayer={selectLayer}
          />
        ) : null}
      </div>
    </section>
  );
}
