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
// — because the lanes scroll and re-flow independently; horizontal scrolling is
// mirrored to every lane so one scroll offset keeps all time axes aligned.

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
  id: number;
  sourceAgentId: number;
  targetAgentId: number;
  ts: string;
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
  label,
}: {
  /** The relative-positioned stack the lanes render in. */
  containerRef: RefObject<HTMLDivElement | null>;
  laneRefs: RefObject<HTMLDivElement | null>[];
  lanes: CompareArrowLane[];
  timeWindow: { from: string; to: string };
  hovered: CompareArrowHover | null;
  onHoverChange: (next: CompareArrowHover | null) => void;
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

  const arrows = useMemo(
    () =>
      specs.flatMap((spec) => {
        const sourceIndex = laneIndexByAgent.get(spec.sourceAgentId);
        const targetIndex = laneIndexByAgent.get(spec.targetAgentId);
        if (sourceIndex === undefined || targetIndex === undefined) return [];
        const sourceBox = laneBoxes[sourceIndex];
        const targetBox = laneBoxes[targetIndex];
        if (!sourceBox || !targetBox) return [];
        const plot = plotGeometry(targetBox.width);
        const x =
          targetBox.left +
          plot.left +
          timeCoordinate(spec.ts, timeWindow.from, timeWindow.to, plot.width);
        const yFrom = sourceBox.top + plot.axisY;
        const yTo = targetBox.top + plot.axisY;
        const midY = (yFrom + yTo) / 2;
        return [
          {
            ...spec,
            hue: COMPARE_LANE_HUES[sourceIndex % COMPARE_LANE_HUES.length],
            path: `M ${x.toFixed(1)} ${yFrom.toFixed(1)} Q ${(x - ARROW_BOW_PX).toFixed(1)} ${midY.toFixed(1)} ${x.toFixed(1)} ${yTo.toFixed(1)}`,
          },
        ];
      }),
    [laneBoxes, laneIndexByAgent, specs, timeWindow.from, timeWindow.to],
  );

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
        const emphasized = hovered?.id === arrow.id;
        const dimmed = hovered !== null && !emphasized;
        return (
          <g key={arrow.id}>
            <path
              data-testid="compare-arrow"
              d={arrow.path}
              fill="none"
              stroke={arrow.hue}
              strokeWidth={emphasized ? ARROW_EMPHASIZED_WIDTH_PX : ARROW_STROKE_WIDTH_PX}
              strokeOpacity={dimmed ? 0.15 : 0.9}
              markerEnd={`url(#compare-arrow-head-${laneIndexByAgent.get(arrow.sourceAgentId)})`}
            />
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
