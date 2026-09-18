"use client";

// Cross-lane message arrows for the multi-agent compare view (task #3735).
//
// One overlay above the stacked lanes draws every chat delivery from
// `inbound_messages` (wire: RunTimelineResponse.inbounds) as a sender-hued
// curve from the sending lane to the receiving lane, at the delivery
// timestamp's x on the shared time axis. Only inbounds whose source is
// another visible lane become arrows; `null`/`undefined` inbounds (a degraded
// read) draw nothing rather than guessing.
//
// Geometry is measured from the lane DOM — each lane's
// [data-testid=run-timeline-visualization] box plus plotGeometry/timeCoordinate
// — because the lanes scroll. The compare view pins ONE shared canvas width
// across all lanes (task #3802), so a delivery timestamp maps to a single x;
// horizontal scrolling is mirrored to every lane so one scroll offset keeps
// all time axes aligned.

import {
  type RefObject,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";

import type { RunTimelineResponse } from "@/lib/types";

import { timeCoordinate } from "./scales";
import { plotGeometry } from "./timeline-layout";

/** Stable, colorblind-checked lane hues; index = lane position. */
export const COMPARE_LANE_HUES = [
  "var(--series-1)",
  "var(--series-3)",
  "var(--series-2)",
  "var(--series-4)",
  "var(--series-5)",
  // Beyond the five themed series vars (task #3802, full-fleet compare):
  // fixed mid-tone hues that stay distinguishable from the series set in
  // both color schemes.
  "#8b5cf6",
  "#0d9488",
  "#db2777",
] as const;

const AGENT_SOURCE_PREFIX = "agent:";
// Bow width of the curve (the pilot v16 look): both endpoints share the
// delivery timestamp's x, the control point pulls the middle sideways.
const ARROW_BOW_PX = 16;
// Invisible hover target width — wide enough to grab, narrow enough not to
// shadow the lane content underneath.
const ARROW_HIT_WIDTH_PX = 12;
const ARROW_STROKE_WIDTH_PX = 1.5;
const ARROW_EMPHASIZED_WIDTH_PX = 2.5;
// Same-pair deliveries within this distance on the shared axis merge into one
// curve with a count badge (task #3802): a burst between two agents reads as
// one arrow, not a stack of identical bows.
export const ARROW_CLUSTER_TOLERANCE_PX = 8;
// Bow-width multipliers, rotated by source→target pair in first-seen order, so
// concurrent deliveries between different pairs fan apart instead of nesting
// exactly on top of each other.
const PAIR_BOW_FACTORS = [1, 0.7, 1.3] as const;

const SCROLL_CONTAINER_SELECTOR = '[data-testid="run-timeline-scroll"]';
const VISUALIZATION_SELECTOR = '[data-testid="run-timeline-visualization"]';

export interface CompareArrowLane {
  agentId: number;
  timeline: RunTimelineResponse | undefined;
}

export interface CompareArrowSpec {
  /** inbound_id — unique across the visible lanes (stable React key). */
  id: number;
  sourceAgentId: number;
  targetAgentId: number;
  ts: string;
}

export interface CompareArrowHover {
  /** The cluster's first member — stable across re-renders. */
  id: number;
  sourceAgentId: number;
  targetAgentId: number;
  ts: string;
  /** 1 for a single delivery; >1 for a merged burst (task #3802). */
  count: number;
}

/** The arrows the lanes imply: chat inbounds sent by another visible lane.
 *  Self-sends, non-agent senders, and senders outside the visible set draw
 *  nothing. */
export function compareArrowSpecs(lanes: CompareArrowLane[]): CompareArrowSpec[] {
  const visible = new Set(lanes.map((lane) => lane.agentId));
  const specs: CompareArrowSpec[] = [];
  for (const lane of lanes) {
    for (const inbound of lane.timeline?.inbounds ?? []) {
      if (!inbound.source.startsWith(AGENT_SOURCE_PREFIX)) continue;
      const sourceAgentId = Number(inbound.source.slice(AGENT_SOURCE_PREFIX.length));
      if (!Number.isInteger(sourceAgentId) || sourceAgentId === lane.agentId) continue;
      if (!visible.has(sourceAgentId)) continue;
      specs.push({
        id: inbound.inbound_id,
        sourceAgentId,
        targetAgentId: lane.agentId,
        ts: inbound.ts,
      });
    }
  }
  return specs.sort((left, right) => Date.parse(left.ts) - Date.parse(right.ts));
}

export interface PlottedArrow {
  spec: CompareArrowSpec;
  /** The delivery timestamp's x on the shared axis, in stack coordinates. */
  x: number;
}

export interface ArrowCluster {
  sourceAgentId: number;
  targetAgentId: number;
  /** The first member's x — the anchor keeps its true delivery position. */
  x: number;
  members: CompareArrowSpec[];
}

/** Merge same-pair arrows within `tolerancePx` on the shared axis (task
 *  #3802): input must be in delivery order; a later member merges into its
 *  pair's most recent cluster while it stays within tolerance of that
 *  cluster's anchor. */
export function clusterArrows(plotted: PlottedArrow[], tolerancePx: number): ArrowCluster[] {
  const clusters: ArrowCluster[] = [];
  const lastByPair = new Map<string, ArrowCluster>();
  for (const item of plotted) {
    const key = `${item.spec.sourceAgentId}:${item.spec.targetAgentId}`;
    const last = lastByPair.get(key);
    if (last && Math.abs(item.x - last.x) <= tolerancePx) {
      last.members.push(item.spec);
      continue;
    }
    const cluster: ArrowCluster = {
      sourceAgentId: item.spec.sourceAgentId,
      targetAgentId: item.spec.targetAgentId,
      x: item.x,
      members: [item.spec],
    };
    clusters.push(cluster);
    lastByPair.set(key, cluster);
  }
  return clusters;
}

interface LaneBox {
  left: number;
  top: number;
  width: number;
}

/** One scroll offset for every lane: with a shared time axis, a lane that
 *  scrolls alone would misalign the axes (and the arrows) on narrow viewports. */
function mirrorScroll(
  laneRefs: RefObject<HTMLDivElement | null>[],
  target: EventTarget | null,
): void {
  if (!(target instanceof HTMLElement) || !target.matches(SCROLL_CONTAINER_SELECTOR)) return;
  const left = target.scrollLeft;
  for (const ref of laneRefs) {
    const scroll = ref.current?.querySelector<HTMLElement>(SCROLL_CONTAINER_SELECTOR);
    if (scroll && scroll !== target && scroll.scrollLeft !== left) {
      scroll.scrollLeft = left;
    }
  }
}

export function CompareArrows({
  containerRef,
  laneRefs,
  lanes,
  timeWindow,
  hovered,
  onHoverChange,
  focusInbound,
  label,
}: {
  /** The relative-positioned stack the lanes render in. */
  containerRef: RefObject<HTMLDivElement | null>;
  laneRefs: RefObject<HTMLDivElement | null>[];
  lanes: CompareArrowLane[];
  timeWindow: { from: string; to: string };
  hovered: CompareArrowHover | null;
  onHoverChange: (next: CompareArrowHover | null) => void;
  /** P4-4 (#4023): the inbound legend category is highlighted — every arrow
   *  draws emphasized even while nothing is hovered (the highlight acts as a
   *  temporary visibility override; hovering one arrow still narrows to it). */
  focusInbound?: boolean;
  label: string;
}) {
  const [laneBoxes, setLaneBoxes] = useState<(LaneBox | null)[]>([]);
  const specs = useMemo(() => compareArrowSpecs(lanes), [lanes]);
  const laneIndexByAgent = useMemo(
    () => new Map(lanes.map((lane, index) => [lane.agentId, index])),
    [lanes],
  );

  const measure = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    const containerBox = container.getBoundingClientRect();
    setLaneBoxes(
      laneRefs.map((ref) => {
        const visualization = ref.current?.querySelector<HTMLElement>(VISUALIZATION_SELECTOR);
        if (!visualization) return null;
        const box = visualization.getBoundingClientRect();
        return {
          left: box.left - containerBox.left,
          top: box.top - containerBox.top,
          width: box.width,
        };
      }),
    );
  }, [containerRef, laneRefs]);

  // The lanes array (and so `specs`) is a fresh identity every render, but the
  // measurement only needs to re-run when the arrows or the window actually
  // change — depending on `specs` itself would re-measure on every render and
  // the fresh laneBoxes state would loop the render.
  const arrowsKey = specs
    .map((spec) => `${spec.id}:${spec.sourceAgentId}:${spec.targetAgentId}:${spec.ts}`)
    .join("|");

  useLayoutEffect(() => {
    measure();
  }, [measure, arrowsKey, timeWindow.from, timeWindow.to]);

  // Each lane wrapper exists from the first render, but a lane's visualization
  // only exists once that lane's chart has data — re-run the observer setup
  // when a lane gains or loses its chart, and observe the lane's own boxes too:
  // opening a lane's detail panel narrows that lane's chart column without
  // necessarily resizing the stack, and a stale overlay would keep the arrows
  // anchored to the old box.
  const lanesKey = lanes
    .map((lane) => `${lane.agentId}:${lane.timeline ? "chart" : "pending"}`)
    .join("|");

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const onScroll = (event: Event) => {
      mirrorScroll(laneRefs, event.target);
      measure();
    };
    container.addEventListener("scroll", onScroll, { capture: true, passive: true });
    window.addEventListener("resize", measure);
    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => measure());
      observer.observe(container);
      for (const ref of laneRefs) {
        const lane = ref.current;
        if (!lane) continue;
        observer.observe(lane);
        const visualization = lane.querySelector(VISUALIZATION_SELECTOR);
        if (visualization) observer.observe(visualization);
      }
    }
    return () => {
      container.removeEventListener("scroll", onScroll, { capture: true });
      window.removeEventListener("resize", measure);
      observer?.disconnect();
    };
  }, [containerRef, laneRefs, lanesKey, measure]);

  const arrows = useMemo(() => {
    // Measured x per delivery, then clustered — the axis conversion happens
    // before merging, so the pixel tolerance stays in screen space.
    const plotted: PlottedArrow[] = [];
    for (const spec of specs) {
      const sourceIndex = laneIndexByAgent.get(spec.sourceAgentId);
      const targetIndex = laneIndexByAgent.get(spec.targetAgentId);
      if (sourceIndex === undefined || targetIndex === undefined) continue;
      const sourceBox = laneBoxes[sourceIndex];
      const targetBox = laneBoxes[targetIndex];
      if (!sourceBox || !targetBox) continue;
      const plot = plotGeometry(targetBox.width);
      // Anchored to the target lane (where the delivery lands); with the
      // shared canvas width the source lane's axis agrees, so one x serves
      // both endpoints — a divergence would surface as a visible bend.
      const x =
        targetBox.left +
        plot.left +
        timeCoordinate(spec.ts, timeWindow.from, timeWindow.to, plot.width);
      plotted.push({ spec, x });
    }
    const clusters = clusterArrows(plotted, ARROW_CLUSTER_TOLERANCE_PX);
    const pairOrder = new Map<string, number>();
    return clusters.flatMap((cluster) => {
      const sourceIndex = laneIndexByAgent.get(cluster.sourceAgentId);
      const targetIndex = laneIndexByAgent.get(cluster.targetAgentId);
      if (sourceIndex === undefined || targetIndex === undefined) return [];
      const sourceBox = laneBoxes[sourceIndex];
      const targetBox = laneBoxes[targetIndex];
      if (!sourceBox || !targetBox) return [];
      const plot = plotGeometry(targetBox.width);
      const yFrom = sourceBox.top + plot.axisY;
      const yTo = targetBox.top + plot.axisY;
      const midY = (yFrom + yTo) / 2;
      const pairKey = `${cluster.sourceAgentId}:${cluster.targetAgentId}`;
      if (!pairOrder.has(pairKey)) pairOrder.set(pairKey, pairOrder.size);
      const bow =
        ARROW_BOW_PX *
        PAIR_BOW_FACTORS[(pairOrder.get(pairKey) ?? 0) % PAIR_BOW_FACTORS.length];
      const x = cluster.x;
      return [
        {
          id: cluster.members[0].id,
          count: cluster.members.length,
          sourceAgentId: cluster.sourceAgentId,
          targetAgentId: cluster.targetAgentId,
          ts: cluster.members[0].ts,
          x,
          hue: COMPARE_LANE_HUES[sourceIndex % COMPARE_LANE_HUES.length],
          path: `M ${x.toFixed(1)} ${yFrom.toFixed(1)} Q ${(x - bow).toFixed(1)} ${midY.toFixed(1)} ${x.toFixed(1)} ${yTo.toFixed(1)}`,
          badgeX: x + 6,
          badgeY: midY + 3,
        },
      ];
    });
  }, [laneBoxes, laneIndexByAgent, specs, timeWindow.from, timeWindow.to]);

  return (
    <svg
      data-testid="compare-arrows"
      role="img"
      aria-label={label}
      className="pointer-events-none absolute inset-0 h-full w-full"
    >
      <defs>
        {COMPARE_LANE_HUES.map((hue, index) => (
          <marker
            key={hue}
            id={`compare-arrow-head-${index}`}
            markerWidth="7"
            markerHeight="7"
            refX="5.4"
            refY="2.6"
            orient="auto"
          >
            <path d="M0,0 L5.2,2.6 L0,5.2" fill="none" stroke={hue} strokeWidth="1.1" strokeLinecap="round" />
          </marker>
        ))}
      </defs>
      {arrows.map((arrow) => {
        const hoveredArrow = hovered?.id === arrow.id;
        // P4-4 (#4023): with the inbound legend highlighted the whole set is
        // the subject — emphasized until a hover narrows the focus to one.
        const emphasized = hoveredArrow || focusInbound === true;
        const dimmed = hovered !== null && !hoveredArrow;
        return (
          <g key={arrow.id}>
            <path
              data-testid="compare-arrow"
              data-ts={arrow.ts}
              data-count={arrow.count}
              data-x={arrow.x.toFixed(1)}
              data-source={arrow.sourceAgentId}
              data-target={arrow.targetAgentId}
              d={arrow.path}
              fill="none"
              stroke={arrow.hue}
              strokeWidth={emphasized ? ARROW_EMPHASIZED_WIDTH_PX : ARROW_STROKE_WIDTH_PX}
              strokeOpacity={dimmed ? 0.15 : 0.9}
              markerEnd={`url(#compare-arrow-head-${laneIndexByAgent.get(arrow.sourceAgentId)})`}
            />
            {arrow.count > 1 ? (
              <text
                data-testid="compare-arrow-count"
                x={arrow.badgeX}
                y={arrow.badgeY}
                fontSize={9}
                fill="var(--muted-foreground)"
              >
                ×{arrow.count}
              </text>
            ) : null}
            <path
              data-testid="compare-arrow-hit"
              d={arrow.path}
              fill="none"
              stroke="transparent"
              strokeWidth={ARROW_HIT_WIDTH_PX}
              style={{ pointerEvents: "stroke" }}
              onPointerEnter={() =>
                onHoverChange({
                  id: arrow.id,
                  sourceAgentId: arrow.sourceAgentId,
                  targetAgentId: arrow.targetAgentId,
                  ts: arrow.ts,
                  count: arrow.count,
                })
              }
              onPointerLeave={() => onHoverChange(null)}
            />
          </g>
        );
      })}
    </svg>
  );
}
