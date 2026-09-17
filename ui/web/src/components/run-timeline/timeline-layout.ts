import type { RunTimelineResponse } from "@/lib/types";

import { timeCoordinate } from "./scales";

const CANVAS_PADDING = 32;
const AXIS_Y = 38;
const EVENT_CHIP_TOP = 54;
const EVENT_CHIP_HEIGHT = 22;
const EVENT_LANE_PITCH = 30;
const EVENT_CHIP_GAP = 8;
const TRACK_HEIGHT = 36;
const TRACK_GAP = 24;
const MIN_TURN_WIDTH = 6;
const LAYER_ROW_HEIGHT = 22;
const LAYER_ROW_GAP = 6;

interface TimelineLayoutInput {
  width: number;
  window: RunTimelineResponse["window"];
  rows: RunTimelineResponse["rows"];
  events: RunTimelineResponse["events"];
  layers?: RunTimelineResponse["layers"];
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

export interface TimelineTickLayout {
  x: number;
  timestamp: string;
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

/** The plot area insets for one canvas width — one source of truth for
 *  geometry consumers outside the chart (the compare-view arrow overlay). */
export function plotGeometry(width: number): { left: number; width: number } {
  const clamped = Math.max(320, Math.round(width));
  return { left: CANVAS_PADDING, width: clamped - CANVAS_PADDING * 2 };
}

function projectedX(
  timestamp: string,
  window: RunTimelineResponse["window"],
  plotLeft: number,
  plotWidth: number,
): number {
  return Math.round(plotLeft + timeCoordinate(timestamp, window.from, window.to, plotWidth));
}

function eventChipWidth(kind: string): number {
  return Math.min(156, Math.max(56, kind.length * 6 + 12));
}

function connectorPath(source: TimelinePoint, destination: TimelinePoint): string {
  const middleY = Math.round((source.y + destination.y) / 2);
  return `M ${source.x} ${source.y} C ${source.x} ${middleY} ${destination.x} ${middleY} ${destination.x} ${destination.y}`;
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
  const windowStart = Date.parse(input.window.from);
  const windowSpan = Date.parse(input.window.to) - windowStart;
  const ticks: TimelineTickLayout[] = Array.from({ length: 5 }, (_, index) => ({
    x: Math.round(plot.left + (plot.width * index) / 4),
    timestamp: new Date(windowStart + (windowSpan * index) / 4).toISOString(),
  }));
  const turns: TimelineTurnLayout[] = input.rows.map((row, rowIndex) => {
    const projectedStartX = projectedX(row.start, input.window, plot.left, plot.width);
    const projectedEndX = projectedX(row.end, input.window, plot.left, plot.width);
    const left = Math.min(projectedStartX, plot.right - MIN_TURN_WIDTH);
    const turnWidth = Math.min(plot.right - left, Math.max(MIN_TURN_WIDTH, projectedEndX - projectedStartX));
    return {
      rowIndex,
      projectedStartX,
      projectedEndX,
      left,
      width: turnWidth,
    };
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
    const blocks = nodes.map(({ node, nodeIndex }) => {
      const startX = projectedX(node.start, input.window, plot.left, plot.width);
      const endX = projectedX(node.end, input.window, plot.left, plot.width);
      const left = Math.min(startX, plot.right - MIN_TURN_WIDTH);
      const width = Math.min(plot.right - left, Math.max(MIN_TURN_WIDTH, endX - startX));
      return { nodeIndex, left, width };
    });
    return { depth, top, height: LAYER_ROW_HEIGHT, blocks };
  });
  const layersBottom =
    layerDepths.length === 0
      ? eventRailBottom
      : layersTop + layerDepths.length * (LAYER_ROW_HEIGHT + LAYER_ROW_GAP) - LAYER_ROW_GAP;
  const trackTop = layersBottom + TRACK_GAP;

  return {
    width,
    height: trackTop + TRACK_HEIGHT + 20,
    axisY: AXIS_Y,
    plot,
    ticks,
    track: { top: trackTop, height: TRACK_HEIGHT },
    layerRows,
    turns,
    events,
    connectors,
  };
}
