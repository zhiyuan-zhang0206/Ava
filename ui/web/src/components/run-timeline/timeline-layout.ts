import { formatTokensCompact } from "@/lib/format-number";
import type { RunTimelineResponse } from "@/lib/types";

import type { TimelineContextView } from "./context-view";
import { tickLabel } from "./run-timeline-details";
import { timeCoordinate } from "./scales";
import {
  buildContextStripLayout,
  buildStripLayout,
  coveredMessageIndexes,
  type StripMessageLayout,
} from "./strip-layout";

const CANVAS_PADDING = 32;
const AXIS_Y = 38;
const EVENT_CHIP_TOP = 54;
const EVENT_CHIP_HEIGHT = 22;
const EVENT_LANE_PITCH = 30;
const EVENT_CHIP_GAP = 8;
const TRACK_HEIGHT = 36;
const TRACK_GAP = 24;
// P4-2 (#4023): the raw-context strip row (demo: a 26px band below the
// blocks). Message bars live inside it; the row is the chart's bottom-most.
const STRIP_ROW_HEIGHT = 26;
const MIN_TURN_WIDTH = 6;
const LAYER_ROW_HEIGHT = 22;
const LAYER_ROW_GAP = 6;
// Pending placeholders (B, task #3981): a merged stretch shorter than this
// reads as stray noise rather than a promised segment; stretches within the
// merge gap join so thin seams between sealed nodes do not fragment the band.
// Frontend policy constants, not user settings.
const PENDING_MIN_SPAN_MS = 3 * 60 * 1000;
const PENDING_MERGE_GAP_MS = 2 * 60 * 1000;

interface TimelineLayoutInput {
  width: number;
  window: RunTimelineResponse["window"];
  rows: RunTimelineResponse["rows"];
  events: RunTimelineResponse["events"];
  layers?: RunTimelineResponse["layers"];
  pending?: RunTimelineResponse["pending"];
  /** P4-2 task #4023: raw-context messages for the bottom strip row; when
   *  present, layer blocks also take their geometry from the covered
   *  messages (D3 — the strip is char-width proportional, so a block is its
   *  first-to-last covered message). */
  messages?: RunTimelineResponse["messages"];
  /** P4-1 task #4023: render the layer stack fine-first (coarse rows move to
   *  the bottom). Placeholders keep riding their host row. */
  flipLayers?: boolean;
  /** P4-2b task #4023: x projection — "time" (default; the fetch window is
   *  the axis) or "context" (messages end to end in character units under a
   *  local viewport). */
  axis?: "time" | "context";
  /** P4-2b task #4023: the char-domain viewport of the context axis
   *  (required in context mode; fail fast when missing). */
  contextView?: TimelineContextView;
}

export interface TimelinePoint {
  x: number;
  y: number;
}

export interface TimelineTurnLayout {
  rowIndex: number;
  projectedStartX: number;
  projectedEndX: number;
  left: number;
  width: number;
}

export interface TimelineLayerBlockLayout {
  nodeIndex: number;
  left: number;
  width: number;
}

export interface TimelineLayerRowLayout {
  depth: number;
  top: number;
  height: number;
  blocks: TimelineLayerBlockLayout[];
}

export interface TimelineStripRowLayout {
  top: number;
  height: number;
  messages: StripMessageLayout[];
}

export interface TimelinePendingSpanLayout {
  index: number;
  start: string;
  end: string;
  left: number;
  width: number;
}

export interface TimelineTickLayout {
  x: number;
  /** Stable render key: the ISO stamp (time) or the char value (context). */
  key: string;
  label: string;
}

export interface TimelineEventLayout {
  eventIndex: number;
  lane: number;
  chipLeft: number;
  chipTop: number;
  chipWidth: number;
  source: TimelinePoint;
  destination: TimelinePoint;
}

export interface TimelineConnectorLayout {
  eventIndex: number;
  source: TimelinePoint;
  destination: TimelinePoint;
  path: string;
}

/** The plot geometry for one canvas width — one source of truth for
 *  geometry consumers outside the chart (the compare-view arrow overlay):
 *  the plot insets, and the time-axis y the event rail hangs off. */
export function plotGeometry(width: number): { left: number; width: number; axisY: number } {
  const clamped = Math.max(320, Math.round(width));
  return { left: CANVAS_PADDING, width: clamped - CANVAS_PADDING * 2, axisY: AXIS_Y };
}

function projectedX(
  timestamp: string,
  window: RunTimelineResponse["window"],
  plotLeft: number,
  plotWidth: number,
): number {
  return Math.round(plotLeft + timeCoordinate(timestamp, window.from, window.to, plotWidth));
}

/** The context axis needs a viewport to project through; fail fast when
 *  the caller selected the axis without one. */
function requireContextView(input: TimelineLayoutInput): TimelineContextView {
  const view = input.contextView;
  if (view === undefined) {
    throw new Error("run-timeline layout: the context axis requires a contextView");
  }
  return view;
}

/** The time axis' uniform clock grid (five ticks); seconds appear when the
 *  grid is finer than a minute. */
function timeTicks(
  window: RunTimelineResponse["window"],
  plot: { left: number; width: number },
): TimelineTickLayout[] {
  const windowStart = Date.parse(window.from);
  const windowSpan = Date.parse(window.to) - windowStart;
  const divisions = 4;
  const includeSeconds = windowSpan / divisions < 60_000;
  return Array.from({ length: divisions + 1 }, (_, index) => {
    const timestamp = new Date(windowStart + (windowSpan * index) / divisions).toISOString();
    return {
      x: Math.round(plot.left + (plot.width * index) / divisions),
      key: timestamp,
      label: tickLabel(timestamp, includeSeconds),
    };
  });
}

/** The context axis' char grid (P4-2b): 1-2-5 steps sized for about eight
 *  divisions across the viewport, labelled by the shared compact formatter. */
function contextTicks(
  view: TimelineContextView,
  plot: { left: number; width: number },
): TimelineTickLayout[] {
  const span = view.to - view.from;
  if (!(span > 0) || !(plot.width > 0)) return [];
  const rough = span / 8;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const step =
    [1, 2, 5]
      .map((multiple) => magnitude * multiple)
      .find((candidate) => candidate >= rough) ?? magnitude * 10;
  const ticks: TimelineTickLayout[] = [];
  for (let index = Math.ceil(view.from / step); index * step <= view.to; index += 1) {
    const value = index * step;
    ticks.push({
      x: Math.round(plot.left + ((value - view.from) / span) * plot.width),
      key: String(value),
      label: formatTokensCompact(value),
    });
  }
  return ticks;
}

/** Clamp an interval to the plot box; null when no part is visible.
 *
 * The context axis maps a viewport over geometry that can fall outside the
 * plot, so an interactive overlay (an HTML button) must clamp exactly where
 * the SVG clip cuts — otherwise an invisible clickable box lives outside
 * the plot (PR #2892 review, generalized for P4-2b). `minWidth` keeps the
 * strip's 1px affordance for bars thinner than a pixel. */
export function clampIntervalToPlot(
  left: number,
  width: number,
  plot: { left: number; width: number },
  minWidth = 0,
): { left: number; width: number } | null {
  const start = Math.max(left, plot.left);
  const end = Math.min(left + Math.max(width, minWidth), plot.left + plot.width);
  return end > start ? { left: start, width: end - start } : null;
}

function eventChipWidth(kind: string): number {
  return Math.min(156, Math.max(56, kind.length * 6 + 12));
}

function connectorPath(source: TimelinePoint, destination: TimelinePoint): string {
  const middleY = Math.round((source.y + destination.y) / 2);
  return `M ${source.x} ${source.y} C ${source.x} ${middleY} ${destination.x} ${middleY} ${destination.x} ${destination.y}`;
}

/** The merged, noise-filtered pending stretches the layer track draws.
 *
 * The server's subtraction is exact but fragmented (seams between sealed
 * nodes return as slivers); merging within PENDING_MERGE_GAP_MS and dropping
 * sub-PENDING_MIN_SPAN_MS remainders keeps the band readable. Pure so the
 * timing contract is directly testable.
 */
export function mergePendingSpans(
  pending: NonNullable<RunTimelineResponse["pending"]>,
): { start: string; end: string }[] {
  const parsed = pending
    .map((span) => ({ start: Date.parse(span.start), end: Date.parse(span.end) }))
    .filter(
      (span) => Number.isFinite(span.start) && Number.isFinite(span.end) && span.end > span.start,
    )
    .sort((left, right) => left.start - right.start);
  const merged: { start: number; end: number }[] = [];
  let last: { start: number; end: number } | undefined;
  for (const span of parsed) {
    if (last && span.start - last.end <= PENDING_MERGE_GAP_MS) {
      last.end = Math.max(last.end, span.end);
    } else {
      last = { ...span };
      merged.push(last);
    }
  }
  return merged
    .filter((span) => span.end - span.start >= PENDING_MIN_SPAN_MS)
    .map((span) => ({
      start: new Date(span.start).toISOString(),
      end: new Date(span.end).toISOString(),
    }));
}

/** One rounded pixel projection shared by SVG geometry and fixed HTML text. */
export function buildTimelineLayout(input: TimelineLayoutInput) {
  const width = Math.max(320, Math.round(input.width));
  const geometry = plotGeometry(width);
  const plot = {
    left: geometry.left,
    right: width - CANVAS_PADDING,
    width: geometry.width,
  };
  const context = input.axis === "context" ? { view: requireContextView(input) } : null;
  // P4-2 (#4023): the strip's message geometry, shared by the strip row and
  // the message-derived layer blocks below. The context axis (P4-2b) swaps
  // the time packing for the linear char layout of the same messages.
  const stripMessages = input.messages ?? [];
  const strip =
    stripMessages.length > 0
      ? context
        ? buildContextStripLayout(stripMessages, context.view, { left: plot.left, width: plot.width })
        : buildStripLayout(stripMessages, input.window, { left: plot.left, width: plot.width })
      : null;
  const ticks = context
    ? contextTicks(context.view, { left: plot.left, width: plot.width })
    : timeTicks(input.window, { left: plot.left, width: plot.width });
  const turns: TimelineTurnLayout[] = input.rows.flatMap((row, rowIndex) => {
    if (context) {
      // P4-2b: a turn spans its covered messages (M5 ruling); a turn with no
      // covered message has no character anchor and is omitted.
      if (strip === null) return [];
      const covered = coveredMessageIndexes(stripMessages, row.start, row.end);
      if (covered.length === 0) return [];
      const first = strip.messages[covered[0]];
      const last = strip.messages[covered[covered.length - 1]];
      const left = first.left;
      const width = last.left + last.width - left;
      return [{ rowIndex, projectedStartX: left, projectedEndX: left + width, left, width }];
    }
    const projectedStartX = projectedX(row.start, input.window, plot.left, plot.width);
    const projectedEndX = projectedX(row.end, input.window, plot.left, plot.width);
    const left = Math.min(projectedStartX, plot.right - MIN_TURN_WIDTH);
    const turnWidth = Math.min(plot.right - left, Math.max(MIN_TURN_WIDTH, projectedEndX - projectedStartX));
    return [
      {
        rowIndex,
        projectedStartX,
        projectedEndX,
        left,
        width: turnWidth,
      },
    ];
  });

  const laneRightEdges: number[] = [];
  const events: TimelineEventLayout[] = input.events.map((event, eventIndex) => {
    const source = { x: projectedX(event.ts, input.window, plot.left, plot.width), y: AXIS_Y };
    const chipWidth = eventChipWidth(event.kind);
    const destinationX = Math.max(
      plot.left + chipWidth / 2,
      Math.min(plot.right - chipWidth / 2, source.x),
    );
    const chipLeft = Math.round(destinationX - chipWidth / 2);
    let lane = laneRightEdges.findIndex((rightEdge) => chipLeft >= rightEdge + EVENT_CHIP_GAP);
    if (lane === -1) {
      lane = laneRightEdges.length;
      laneRightEdges.push(0);
    }
    laneRightEdges[lane] = chipLeft + chipWidth;
    const chipTop = EVENT_CHIP_TOP + lane * EVENT_LANE_PITCH;
    return {
      eventIndex,
      lane,
      chipLeft,
      chipTop,
      chipWidth,
      source,
      destination: { x: Math.round(destinationX), y: chipTop },
    };
  });
  const connectors: TimelineConnectorLayout[] = events.map((event) => ({
    eventIndex: event.eventIndex,
    source: event.source,
    destination: event.destination,
    path: connectorPath(event.source, event.destination),
  }));
  const eventRailBottom =
    laneRightEdges.length === 0
      ? AXIS_Y
      : EVENT_CHIP_TOP + (laneRightEdges.length - 1) * EVENT_LANE_PITCH + EVENT_CHIP_HEIGHT;
  const layerNodes = input.layers ?? [];
  const layerDepths = [...new Set(layerNodes.map((node) => node.depth))].sort((a, b) => a - b);
  const layersTop = eventRailBottom + TRACK_GAP;
  const layerRows: TimelineLayerRowLayout[] = layerDepths.map((depth, rowIndex) => {
    const nodes = layerNodes
      .map((node, nodeIndex) => ({ node, nodeIndex }))
      .filter(({ node }) => node.depth === depth)
      .sort((a, b) => Date.parse(a.node.start) - Date.parse(b.node.start));
    const top = layersTop + rowIndex * (LAYER_ROW_HEIGHT + LAYER_ROW_GAP);
    const blocks = nodes.flatMap(({ node, nodeIndex }) => {
      // D3 (#4023): a block spans its first-to-last covered message, so it
      // lines up with the char-width strip it summarizes. A node covering no
      // placeable message (no strip, or only legacy/unplaceable stamps)
      // falls back to the plain time projection — except on the context
      // axis, where that fallback does not exist: a node with no covered
      // message has no character anchor and is omitted (P4-2b).
      if (strip) {
        const covered = coveredMessageIndexes(stripMessages, node.start, node.end);
        if (covered.length > 0) {
          const first = strip.messages[covered[0]];
          const last = strip.messages[covered[covered.length - 1]];
          if (context) {
            // Raw view-mapped extent — the plot clip cuts it and the button
            // layer clamps; clamping here would forge edge slivers for
            // off-view blocks.
            const left = first.left;
            const width = last.left + last.width - left;
            return [{ nodeIndex, left, width }];
          }
          const left = Math.min(first.left, plot.right - MIN_TURN_WIDTH);
          const width = Math.min(
            plot.right - left,
            Math.max(MIN_TURN_WIDTH, last.left + last.width - left),
          );
          return [{ nodeIndex, left, width }];
        }
      }
      if (context) return [];
      const startX = projectedX(node.start, input.window, plot.left, plot.width);
      const endX = projectedX(node.end, input.window, plot.left, plot.width);
      const left = Math.min(startX, plot.right - MIN_TURN_WIDTH);
      const width = Math.min(plot.right - left, Math.max(MIN_TURN_WIDTH, endX - startX));
      return [{ nodeIndex, left, width }];
    });
    return { depth, top, height: LAYER_ROW_HEIGHT, blocks };
  });
  const pendingSpans = mergePendingSpans(input.pending ?? []);
  const pendingBlocks: TimelinePendingSpanLayout[] = pendingSpans.map((span, index) => {
    const startX = projectedX(span.start, input.window, plot.left, plot.width);
    const endX = projectedX(span.end, input.window, plot.left, plot.width);
    const left = Math.min(startX, plot.right - MIN_TURN_WIDTH);
    const width = Math.min(plot.right - left, Math.max(MIN_TURN_WIDTH, endX - startX));
    return { index, start: span.start, end: span.end, left, width };
  });
  // P4-1 (#4023, demo parity): flip reverses the stack top-to-bottom by
  // re-assigning row tops. The host row of the placeholders keeps its
  // identity (by depth), so a flip moves the band with its row instead of
  // re-targeting another row.
  const orderedLayerRows: TimelineLayerRowLayout[] = input.flipLayers
    ? [...layerRows].reverse().map((layerRow, rowIndex) => ({
        ...layerRow,
        top: layersTop + rowIndex * (LAYER_ROW_HEIGHT + LAYER_ROW_GAP),
      }))
    : layerRows;
  // Placeholders ride the first layer row; with no sealed rows they get one
  // synthesized row so the layer band still renders (B spec, 2026-09-18).
  const pendingHostDepth = layerRows.length > 0 ? layerRows[0].depth : null;
  const pendingHostRow =
    pendingHostDepth === null
      ? undefined
      : orderedLayerRows.find((layerRow) => layerRow.depth === pendingHostDepth);
  const pendingRow = {
    top: pendingHostRow?.top ?? layersTop,
    height: LAYER_ROW_HEIGHT,
  };
  const layersBottom =
    layerDepths.length === 0
      ? pendingBlocks.length > 0
        ? layersTop + LAYER_ROW_HEIGHT
        : eventRailBottom
      : layersTop + layerDepths.length * (LAYER_ROW_HEIGHT + LAYER_ROW_GAP) - LAYER_ROW_GAP;
  const trackTop = layersBottom + TRACK_GAP;

  return {
    width,
    height:
      strip === null
        ? trackTop + TRACK_HEIGHT + 20
        : trackTop + TRACK_HEIGHT + TRACK_GAP + STRIP_ROW_HEIGHT + 20,
    axisY: AXIS_Y,
    plot,
    ticks,
    track: { top: trackTop, height: TRACK_HEIGHT },
    strip:
      strip === null
        ? null
        : {
            top: trackTop + TRACK_HEIGHT + TRACK_GAP,
            height: STRIP_ROW_HEIGHT,
            messages: strip.messages,
          },
    layerRows: orderedLayerRows,
    pendingRow,
    pendingBlocks,
    turns,
    events,
    connectors,
  };
}
