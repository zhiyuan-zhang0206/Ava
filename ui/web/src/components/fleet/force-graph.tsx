// Force Graph — the shared force-directed canvas behind the fleet Graph View
// (agent nodes) and the Task Graph (task nodes).
//
// One rendering / interaction / parameter system for both graphs so they can
// never drift: the same d3-force physics (useForceLayout), the same
// zoom/pan/reset/focus interactions, the same edge styling, selection ring,
// hover treatment, ForceControls gear, stats bar and zoom reset. The ONLY
// visual difference between the two views is node shape — "circle" for agents,
// "square" for tasks — everything else is this one component.
//
// Display data (status color, score, badge) is always read from the latest
// props on render — the simulation only owns positions, so live SSE updates
// recolor/resize nodes in place without disturbing the layout.

"use client";

import { select } from "d3-selection";
import {
  zoom as d3Zoom,
  zoomIdentity,
  type D3ZoomEvent,
  type ZoomBehavior,
  type ZoomTransform,
} from "d3-zoom";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { useForceLayout, type Pos, type SimLink, type SimNode } from "@/lib/use-force-layout";
import { cn } from "@/lib/utils";

import { ForceControls, FORCE_GROUPS, type ForceGroup, type ForceParams } from "./force-controls";
import { FLEX, OVERFLOW_HIDDEN } from "@/lib/layout";

// ── Shared node / edge model ──
// Each view adapts its own data (FleetGraphNode / TaskRow) into this shape.
// `score` is the node-size driver, normalized against the graph's max so the
// band always spreads across the panel (agents: recent-work score; tasks:
// uniform — every task node passes score 0 per user ruling 2026-08-09 #1070,
// so the band collapses to the minimum radius). `badge` is the needs-you
// overlay (tasks only). `dashed` marks fork lineage edges in the agent graph.

export interface ForceGraphNode {
  readonly id: number;
  /** Second text line inside the node (agent label / task title). */
  readonly label: string | null;
  /** Status key → the `statusText` color class passed by the view. */
  readonly status: string;
  /** Node-size driver; normalized against the graph max (0 → min size). */
  readonly score: number;
  readonly pulse?: boolean;
  /** Needs-you overlay badge (count + priority tone class). */
  readonly badge?: { count: number; tone: string } | null;
  /** Native SVG hover title — full detail without cluttering the node. */
  readonly nodeTitle?: string | null;
}

export interface ForceGraphEdge {
  readonly from: number;
  readonly to: number;
  /** "lineage" = spawn/fork/resurrect (agents) / parent-child (tasks). */
  readonly kind: "lineage" | "message";
  readonly weight: number;
  readonly dashed?: boolean;
}

// Node size: radius = minR + (maxR - minR) * sqrt(score / maxScore). sqrt
// spreads the mid-range evenly; a node with score 0 sits at minR.
export function radiusOf(score: number, maxScore: number, minR: number, maxR: number): number {
  const ratio = maxScore > 0 ? score / maxScore : 0;
  return minR + (maxR - minR) * Math.sqrt(ratio);
}

// Zoom bounds (scale factor on the content <g>) — shared by both graphs.
const ZOOM_MIN = 0.15;
const ZOOM_MAX = 4;

// Label truncation: the text block must fit inside the node with padding. 6px
// mono glyphs are ~3.6px wide; the budget is the node's diameter minus padding.
function fitLabel(label: string, r: number): string {
  const maxChars = Math.floor((2 * r - 8) / 3.6);
  return label.length > maxChars ? label.slice(0, Math.max(maxChars - 1, 0)) + "…" : label;
}

export const ForceGraph = memo(function ForceGraph({
  nodes,
  edges,
  shape,
  statusText,
  selectedId,
  onSelect,
  onOpen,
  params,
  setParams,
  resetParams,
  groups = FORCE_GROUPS,
  statsText,
  overlayLeft,
  ariaLabel = "Fleet graph",
}: {
  nodes: readonly ForceGraphNode[];
  edges: readonly ForceGraphEdge[];
  shape: "circle" | "square";
  /** Status key → text-* color class (the node paints with fill="currentColor"). */
  statusText: Record<string, string>;
  selectedId: number | null;
  onSelect: (id: number | null) => void;
  /** Double-click on a node (agents: their timeline; tasks: the owner's). */
  onOpen: (id: number) => void;
  params: ForceParams;
  setParams: (p: ForceParams) => void;
  resetParams: () => void;
  groups?: ForceGroup[];
  /** Stats bar text (bottom-left); omitted → no bar. */
  statsText?: string | null;
  /** Extra control rendered beside the layout gear (e.g. the window selector). */
  overlayLeft?: ReactNode;
  ariaLabel?: string;
}) {
  // Max score across the graph — the radius normalizer. Must match the value
  // used for the sim collide radii so the rendered node matches its collision
  // circle. floor 1 avoids div-by-zero when every score is 0.
  const maxScore = useMemo(() => Math.max(...nodes.map((n) => n.score), 1), [nodes]);
  // Edge opacity spans this graph's actual weight range, so the weakest and
  // strongest relationships remain visually distinct as the graph changes.
  const maxEdgeWeight = useMemo(() => Math.max(...edges.map((e) => e.weight), 1), [edges]);
  const nodeById = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);

  // Simulation nodes: collide radius = the rendered radius for circles, the
  // half-diagonal (r·√2) for squares — a square claims its full footprint.
  // Recomputed on params change so the Node sliders resize the collision
  // circles live (useForceLayout tracks radii in a ref).
  const simNodes = useMemo<SimNode[]>(
    () =>
      nodes.map((n) => ({
        id: n.id,
        r: radiusOf(n.score, maxScore, params.nodeSizeMin, params.nodeSizeMax) * (shape === "square" ? Math.SQRT2 : 1),
      })),
    [nodes, maxScore, params.nodeSizeMin, params.nodeSizeMax, shape],
  );
  const simLinks = useMemo<SimLink[]>(
    () => edges.map((e) => ({ source: e.from, target: e.to })),
    [edges],
  );
  const { positions, layout } = useForceLayout(simNodes, simLinks, params);

  const [hovered, setHovered] = useState<{ id: number; x: number; y: number } | null>(null);
  // Throttle onMouseMove → React setState to ~20 Hz so rapid cursor movement
  // doesn't trigger per-pixel re-renders just for hover opacity.
  const hoverThrottleRef = useRef(0);

  // User zoom/pan as an SVG transform on the content <g> (translate + scale),
  // separate from the fit-to-content viewBox base frame. d3-zoom owns wheel /
  // pinch / drag; programmatic transforms (reset, focus) flow through the same
  // "zoom" handler. identity transform == fit-to-content.
  const svgRef = useRef<SVGSVGElement | null>(null);
  const zoomRef = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const [transform, setTransform] = useState<ZoomTransform>(zoomIdentity);
  // Animate the <g> only for programmatic transforms (event.sourceEvent == null);
  // interactive wheel/drag must track the cursor 1:1 with no transition lag.
  const [animateZoom, setAnimateZoom] = useState(false);

  // Direct ref mirrors kept current via useLayoutEffect so focusNode never
  // reads a stale Map / box (the layout effect can lag by one microtask).
  const positionsRef = useRef(positions);
  useLayoutEffect(() => {
    positionsRef.current = positions;
  });
  const layoutRef = useRef<{ positions: Map<number | string, Pos>; cx: number; cy: number; w: number; h: number }>({
    positions,
    cx: 0,
    cy: 0,
    w: 200,
    h: 200,
  });
  const extentRef = useRef<[[number, number], [number, number]]>([
    [-120, -120],
    [120, 120],
  ]);
  useLayoutEffect(() => {
    if (!layout) return;
    layoutRef.current = {
      positions,
      cx: layout.minX + layout.w / 2,
      cy: layout.minY + layout.h / 2,
      w: layout.w,
      h: layout.h,
    };
    extentRef.current = [
      [layout.minX, layout.minY],
      [layout.minX + layout.w, layout.minY + layout.h],
    ];
  }, [layout, positions]);

  // Install the d3-zoom behavior via a callback ref so it attaches the moment
  // the <svg> actually mounts (the svg only renders once the layout settles, so
  // an effect keyed on mount would miss it). The extent accessor returns our
  // own box (never reads the DOM viewBox), so wheel-to-cursor centering stays
  // correct under a scaled viewBox. dblclick.zoom is removed so a double-click
  // on a node doesn't fight the node's own double-click handler.
  const attachZoom = useCallback((svg: SVGSVGElement | null) => {
    svgRef.current = svg;
    if (!svg) {
      zoomRef.current = null;
      return;
    }
    const behavior = d3Zoom<SVGSVGElement, unknown>()
      .scaleExtent([ZOOM_MIN, ZOOM_MAX])
      .extent(() => extentRef.current)
      .on("zoom", (event: D3ZoomEvent<SVGSVGElement, unknown>) => {
        setAnimateZoom(event.sourceEvent == null);
        setTransform(event.transform);
      });
    zoomRef.current = behavior;
    select(svg).call(behavior).on("dblclick.zoom", null);
  }, []);

  // Precomputed neighbor sets (node id → neighbor ids) for the zoom bounding
  // box when focusing a node.
  const neighborMap = useMemo(() => {
    const map = new Map<number, number[]>();
    for (const e of edges) {
      let a = map.get(e.from);
      if (!a) {
        a = [];
        map.set(e.from, a);
      }
      a.push(e.to);
      let b = map.get(e.to);
      if (!b) {
        b = [];
        map.set(e.to, b);
      }
      b.push(e.from);
    }
    return map;
  }, [edges]);

  // Center + zoom the given node to the middle of the viewBox (CSS-animated,
  // since a programmatic transform has no sourceEvent). No-op until the node
  // settles.
  const focusNode = useCallback(
    (id: number) => {
      const svg = svgRef.current;
      const behavior = zoomRef.current;
      if (!svg || !behavior) return;
      const { positions: pos } = layoutRef.current;
      // Prefer the direct positionsRef (kept current via useLayoutEffect) for
      // the node coordinate; fall back to the layoutRef copy.
      const p = positionsRef.current.get(id) ?? pos.get(id);
      if (!p) return;

      // Collect directly connected neighbor ids for bounding-box zoom.
      const neighbors = neighborMap.get(id) ?? [];

      // Bounding box of selected node + directly connected nodes.
      let minX = p.x, minY = p.y, maxX = p.x, maxY = p.y;
      for (const nid of neighbors) {
        const np = pos.get(nid);
        if (np) {
          if (np.x < minX) minX = np.x;
          if (np.y < minY) minY = np.y;
          if (np.x > maxX) maxX = np.x;
          if (np.y > maxY) maxY = np.y;
        }
      }

      // Padding so nodes aren't clipped at the viewport edges.
      const pad = params.nodeSizeMax + params.zoomPadding;
      minX -= pad;
      minY -= pad;
      maxX += pad;
      maxY += pad;

      const boxW = maxX - minX || 1;
      const boxH = maxY - minY || 1;
      const boxCx = (minX + maxX) / 2;
      const boxCy = (minY + maxY) / 2;

      // Scale to fit the bounding box within the viewBox (clamped to zoom bounds).
      const { cx, cy, w, h } = layoutRef.current;
      const fitScale = Math.min(w / boxW, h / boxH, ZOOM_MAX) * params.zoomFitRatio;
      const scale = Math.max(ZOOM_MIN, fitScale);

      // Transform: center the bounding-box midpoint at the viewBox center.
      // SVG transform "translate(tx, ty) scale(s)": (x, y) → (x * s + tx, y * s + ty)
      // We want (boxCx, boxCy) → (cx, cy): tx = cx - boxCx * s, ty = cy - boxCy * s
      const tx = cx - boxCx * scale;
      const ty = cy - boxCy * scale;

      behavior.transform(select(svg), zoomIdentity.translate(tx, ty).scale(scale));
    },
    [neighborMap, params.nodeSizeMax, params.zoomPadding, params.zoomFitRatio],
  );

  const resetZoom = useCallback(() => {
    const svg = svgRef.current;
    const behavior = zoomRef.current;
    if (svg && behavior) behavior.transform(select(svg), zoomIdentity);
  }, []);

  // Auto-focus the selected node whenever it changes (e.g. from the
  // Decisions/Reviews panel or the other graph). Fire once per selection change
  // (not every layout tick, so it never fights a user pan), retried via the
  // positions dep until that node has a settled position.
  const focusedRef = useRef<number | null>(null);
  useEffect(() => {
    if (selectedId == null) {
      focusedRef.current = null;
      return;
    }
    if (focusedRef.current === selectedId) return;
    if (!positions.has(selectedId)) return;
    focusedRef.current = selectedId;
    focusNode(selectedId);
  }, [selectedId, positions, focusNode]);

  // Nodes directly connected to the selection (via any edge) — these stay lit
  // while the rest dims.
  const connectedNodeIds = useMemo(() => {
    if (selectedId == null) return null;
    const ids = new Set<number>();
    for (const e of edges) {
      if (e.from === selectedId) ids.add(e.to);
      if (e.to === selectedId) ids.add(e.from);
    }
    return ids;
  }, [edges, selectedId]);

  // The views show their own empty / loading placeholders; the shell (stats
  // bar + controls) still renders so an empty canvas keeps its chrome.
  const { placed, minX, minY, w, h } = layout ?? {
    placed: [],
    minX: 0,
    minY: 0,
    w: 0,
    h: 0,
  };

  return (
    <div className={cn("relative h-full w-full", OVERFLOW_HIDDEN)}>
      {layout ? (
      <svg
        ref={attachZoom}
        className="h-full w-full select-none touch-none"
        viewBox={`${minX} ${minY} ${w} ${h}`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={ariaLabel}
        onClick={(ev) => {
          // Clicking the SVG background (not a node) deselects.
          if (ev.target === ev.currentTarget) onSelect(null);
        }}
      >
        {/* All graph content lives in one zoom/pan layer: the user transform
            (translate + scale) applies here. CSS-animated only for programmatic
            focus/reset (animateZoom); instant for interactive wheel/drag. */}
        <g
          transform={transform.toString()}
          style={{ transition: animateZoom ? "transform 0.4s ease" : "none" }}
          onTransitionEnd={(ev) => {
            // After the focus/reset zoom animation finishes, disable the CSS
            // transition so that subsequent simulation-tick re-renders (which
            // don't change the transform) never trigger a stray transition.
            if (ev.propertyName === "transform") setAnimateZoom(false);
          }}
        >
          {/* Edges. */}
          <g>
            {edges.map((e, i) => {
              const a = positions.get(e.from);
              const b = positions.get(e.to);
              if (!a || !b) return null;
              const isMessage = e.kind === "message";
              // Selection highlight: incident edges get full opacity + thicker
              // stroke; non-incident edges get heavily dimmed.
              const isIncident =
                connectedNodeIds != null &&
                (e.from === selectedId || e.to === selectedId);
              const isDimmed = connectedNodeIds != null && !isIncident;
              const normalizedWeight = Math.min(Math.max(e.weight / maxEdgeWeight, 0), 1);
              const opacity = isIncident
                ? 0.92
                : isDimmed
                  ? 0.08
                  : isMessage
                    ? 0.24 + 0.61 * normalizedWeight
                    : 0.72;
              // Stroke width scales with weight (log-compressed so high-weight
              // edges don't overwhelm). Base width depends on edge type, then
              // weight adds on top.
              const weightSw = Math.sqrt(e.weight) * 1.2;
              const sw = isIncident
                ? (isMessage ? 2.0 : 2.8) + weightSw
                : (isMessage ? 0.6 : 1.2) + weightSw;
              // Index suffix keeps the key unique even if a view ever passes two
              // edges with the same (from, to, kind) — duplicate keys make
              // reconciliation orphan DOM <line> nodes on every layout tick
              // (stale edges accumulate in space).
              return (
                <line
                  key={`${e.from}-${e.to}-${e.kind}-${i}`}
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  // One gray for every edge kind (user ruling 2026-08-05: no
                  // blue message edges — lineage and message ties both render
                  // muted gray; kind is still legible via opacity/width).
                  className="text-muted-foreground"
                  stroke="currentColor"
                  strokeOpacity={opacity}
                  strokeWidth={sw}
                  strokeDasharray={e.dashed ? "4 3" : undefined}
                />
              );
            })}
          </g>

          {/* Nodes. */}
          <g>
            {placed.map(({ node: sim, p }) => {
              const n = nodeById.get(sim.id as number);
              if (!n) return null;
              const r = radiusOf(n.score, maxScore, params.nodeSizeMin, params.nodeSizeMax);
              const isHovered = hovered?.id === n.id;
              const isRinged = selectedId === n.id;
              const fill = statusText[n.status] ?? "text-slate-400";
              const label = n.label ? fitLabel(n.label, r) : null;
              const badgeR = Math.max(5, r * 0.4);
              return (
                <g
                  key={n.id}
                  transform={`translate(${p.x},${p.y})`}
                  className="cursor-pointer"
                  onPointerDown={(ev) => {
                    // Prevent d3-zoom from capturing the pointer; a flicker
                    // occurs when d3-zoom starts a drag gesture on a node
                    // even though no zoom/pan actually happens (mousedown
                    // without movement). The pointer event never reaches the
                    // SVG, so d3-zoom stays dormant for node interactions.
                    ev.stopPropagation();
                  }}
                  onClick={(ev) => {
                    ev.stopPropagation();
                    // Clicking an already-selected node is a no-op.
                    if (selectedId === n.id) return;
                    onSelect(n.id);
                    focusNode(n.id);
                  }}
                  onDoubleClick={(ev) => {
                    ev.stopPropagation();
                    onOpen(n.id);
                  }}
                  onMouseEnter={(ev) => setHovered({ id: n.id, x: ev.clientX, y: ev.clientY })}
                  onMouseMove={(ev) => {
                    const now = Date.now();
                    if (now - hoverThrottleRef.current < 50) return;
                    hoverThrottleRef.current = now;
                    setHovered({ id: n.id, x: ev.clientX, y: ev.clientY });
                  }}
                  onMouseLeave={() => setHovered((cur) => (cur?.id === n.id ? null : cur))}
                >
                  {n.nodeTitle ? <title>{n.nodeTitle}</title> : null}
                  {isRinged ? (
                    shape === "circle" ? (
                      <circle
                        r={r + 6}
                        className="text-sky-400 animate-pulse"
                        fill="none"
                        stroke="currentColor"
                        strokeOpacity={0.7}
                        strokeWidth={2}
                        strokeDasharray="4 3"
                      />
                    ) : (
                      <rect
                        x={-(r + 6)}
                        y={-(r + 6)}
                        width={2 * (r + 6)}
                        height={2 * (r + 6)}
                        rx={r * 0.25}
                        className="text-sky-400 animate-pulse"
                        fill="none"
                        stroke="currentColor"
                        strokeOpacity={0.7}
                        strokeWidth={2}
                        strokeDasharray="4 3"
                      />
                    )
                  ) : null}
                  {shape === "circle" ? (
                    <circle
                      r={r}
                      className={cn(fill, n.pulse && "animate-pulse")}
                      fill="currentColor"
                      stroke="var(--background)"
                      strokeWidth={1.5}
                      opacity={isHovered ? 1 : 0.92}
                    />
                  ) : (
                    <rect
                      x={-r}
                      y={-r}
                      width={2 * r}
                      height={2 * r}
                      rx={r * 0.25}
                      className={cn(fill, n.pulse && "animate-pulse")}
                      fill="currentColor"
                      stroke="var(--background)"
                      strokeWidth={1.5}
                      opacity={isHovered ? 1 : 0.92}
                    />
                  )}
                  <text
                    y={label ? -3.5 : 0}
                    textAnchor="middle"
                    dominantBaseline="central"
                    className="fill-white text-[6px] font-mono font-semibold"
                    stroke="rgba(0,0,0,0.35)"
                    strokeWidth={0.75}
                    paintOrder="stroke"
                    style={{ pointerEvents: "none" }}
                  >
                    <tspan x={0} dominantBaseline="central">
                      #{n.id}
                    </tspan>
                    {label ? (
                      <tspan x={0} dy={7} dominantBaseline="central">
                        {label}
                      </tspan>
                    ) : null}
                  </text>
                  {/* Needs-you badge — pending require_response notices on this
                      node's owner, colored by top priority (tasks only). */}
                  {n.badge != null && n.badge.count > 0 ? (
                    <g aria-label={`${n.badge.count} waiting on you`}>
                      <circle
                        cx={r - 1}
                        cy={-(r - 1)}
                        r={badgeR}
                        className={n.badge.tone}
                        style={{ fill: "currentColor" }}
                      />
                      <text
                        x={r - 1}
                        y={-(r - 1)}
                        textAnchor="middle"
                        dy={3}
                        className="fill-white text-[9px] font-bold tabular-nums"
                      >
                        {n.badge.count}
                      </text>
                    </g>
                  ) : null}
                </g>
              );
            })}
          </g>
        </g>
      </svg>
      ) : nodes.length > 0 ? (
        <div className={cn("h-full items-center justify-center text-xs text-muted-foreground", FLEX)}>
          Loading...
        </div>
      ) : null}

      {/* Stats bar: node / edge counts. */}
      {statsText != null ? (
        <div className="pointer-events-none absolute bottom-3 left-3 rounded-md border border-border bg-background/80 px-3 py-1.5 text-[10px] text-muted-foreground tabular-nums backdrop-blur">
          {statsText}
        </div>
      ) : null}

      {/* Toolbar — inset from the canvas edge; wheel/pinch zoom and drag pan
          live on the SVG itself, the reset button returns to the
          fit-to-content frame. Order (user ruling 2026-08-06): settings,
          reset zoom, then view extras (the time-window selector). */}
      <div className={cn("pointer-events-auto absolute left-3 top-3 items-center gap-1", FLEX)}>
        <ForceControls params={params} setParams={setParams} reset={resetParams} groups={groups} />
        <button
          type="button"
          className={cn("size-6 items-center justify-center rounded border border-border bg-background/80 text-[10px] text-muted-foreground backdrop-blur hover:bg-sidebar-accent hover:text-foreground", FLEX)}
          aria-label="Reset zoom"
          onClick={resetZoom}
          disabled={transform.k === 1 && transform.x === 0 && transform.y === 0}
        >
          ↺
        </button>
        {overlayLeft}
      </div>
    </div>
  );
});
