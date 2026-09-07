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
// Display data (status color, score, ghost flag) is always read from the latest
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
import { useTranslations } from "next-intl";
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
// so the band collapses to the minimum radius). `ghost` marks structurally
// required hidden parents (tasks only) — rendered dimmed with a dashed
// outline. `dashed` marks fork lineage edges in the agent graph.

export interface ForceGraphNode {
  readonly id: number;
  /** Second text line inside the node (agent label / task title). */
  readonly label: string | null;
  /** Status key → the `statusText` color class passed by the view. */
  readonly status: string;
  /** Node-size driver; normalized against the graph max (0 → min size). */
  readonly score: number;
  readonly pulse?: boolean;
  /** Structural ghost node: a hidden parent kept on the canvas to connect
      the tree (e.g. a done parent whose child is still visible). Rendered
      dimmed with a dashed outline. */
  readonly ghost?: boolean;
  /** Native SVG hover title — full detail without cluttering the node. */
  readonly nodeTitle?: string | null;
}

/**
 * Optional per-view hover detail card, rendered the instant the cursor enters
 * a node (no delay). Replaces the native SVG <title>, whose appearance the
 * browser defers (typically ~0.5–1s) — the perceived "hover lag" of the old
 * tooltip. The card must return content for every node it is offered (null
 * leaves the node with no hover surface); it is anchored statically to the
 * hovered node's on-screen box (no cursor following) and flips to stay inside
 * the canvas. Views without a card keep the native title as their fallback.
 */
export type HoverCard = (node: ForceGraphNode) => ReactNode | null;

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

// Zoom floor only — scale factor on the content <g>, shared by both graphs.
// No upper bound (user ruling 2026-08-25: zoom must never be capped); d3
// clamps wheel zoom to this extent, so the non-functional floor keeps k
// strictly positive and prevents a degenerate zero-area transform.
const ZOOM_MIN = 0.001;
const ZOOM_MAX = Infinity;
export const LABEL_MIN_ZOOM = 0.45;

// Label wrapping: the 6px mono font advances ~0.6em per Latin glyph but a full
// em (~6px) per CJK / full-width glyph, so wrapping budgets by measured width,
// not char count — a char-count budget lets CJK labels run past the node edge
// (QA #651: Chinese task titles bled ~20px beyond the circle). Horizontal and
// vertical padding keep every rendered line inside the node; the id occupies
// line one.
export const LABEL_LINE_HEIGHT = 7;

// Per-glyph advance of the 6px mono font: ~0.6em for Latin/digit glyphs, 1em
// for CJK and other full-width glyphs (incl. CJK punctuation and emoji).
export const GLYPH_WIDTH_LATIN = 3.6;
export const GLYPH_WIDTH_FULL = 6;
// Full-width ranges: CJK radicals/kana/hangul/ideographs, CJK punctuation,
// fullwidth forms, emoji/symbol blocks, and the ellipsis we append ourselves.
const FULL_WIDTH_RE =
  /[\u2E80-\u303F\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF\uFF00-\uFFEF\u2600-\u27BF\u{1F000}-\u{1FAFF}\u2026]/u;

function glyphWidth(ch: string): number {
  return FULL_WIDTH_RE.test(ch) ? GLYPH_WIDTH_FULL : GLYPH_WIDTH_LATIN;
}

/** Number of leading chars of `text` that fit within `maxWidth` px. */
function charsThatFit(text: string, maxWidth: number): number {
  let width = 0;
  let i = 0;
  for (const ch of text) {
    const w = glyphWidth(ch);
    // 1e-9 tolerance: summing 3.6px advances in doubles drifts (~1e-14/char),
    // so an exact-budget line (e.g. 10 × 3.6 = 36) must not be cut one glyph
    // short by accumulated float error.
    if (width + w > maxWidth + 1e-9) break;
    width += w;
    i += ch.length;
  }
  return i;
}

export function wrapLabel(label: string, r: number): string[] {
  const maxLineWidth = 2 * r - 8;
  const maxLabelLines = Math.max(Math.floor((2 * r - 4) / LABEL_LINE_HEIGHT) - 1, 0);
  if (maxLineWidth < GLYPH_WIDTH_LATIN || maxLabelLines === 0 || label.length === 0) return [];

  const lines: string[] = [];
  let remaining = label.trim();
  while (charsThatFit(remaining, maxLineWidth) < remaining.length) {
    const cut = charsThatFit(remaining, maxLineWidth);
    if (cut <= 0) break; // one glyph is wider than the line — can't make progress

    if (remaining[cut] === " ") {
      lines.push(remaining.slice(0, cut));
      remaining = remaining.slice(cut + 1).trimStart();
      continue;
    }

    const candidate = remaining.slice(0, cut);
    const spaceIndex = candidate.lastIndexOf(" ");
    const hyphenIndex = candidate.lastIndexOf("-");
    const breakIndex = Math.max(spaceIndex, hyphenIndex);
    if (breakIndex > 0) {
      const includeBreak = candidate[breakIndex] === "-";
      lines.push(candidate.slice(0, breakIndex + (includeBreak ? 1 : 0)).trimEnd());
      remaining = remaining.slice(breakIndex + 1).trimStart();
      continue;
    }

    // No space/hyphen on this line: `cut` falls inside a narrow (Latin/digit)
    // run. When that run started after a full-width prefix, break at the run's
    // start instead of splitting the word — a mixed label like
    // "\u8d44\u6e90\u76d1\u63a7\uff08Cluster Ops \u57df\uff09" must not render "\u8d44\u6e90\u76d1\u63a7\uff08C" / "luster Ops"
    // (QA #651 deploy verification). Only split a run when it starts the line,
    // i.e. the word itself is wider than the line budget.
    if (cut > 0 && !FULL_WIDTH_RE.test(remaining[cut - 1])) {
      let runStart = cut - 1;
      while (runStart > 0 && !FULL_WIDTH_RE.test(remaining[runStart - 1])) runStart--;
      if (runStart > 0) {
        lines.push(remaining.slice(0, runStart).trimEnd());
        remaining = remaining.slice(runStart).trimStart();
        continue;
      }
    }

    lines.push(remaining.slice(0, cut));
    remaining = remaining.slice(cut);
  }
  if (remaining.length > 0) lines.push(remaining);
  if (lines.length <= maxLabelLines) return lines;

  // Ellipsize: the last visible line makes room for the ellipsis glyph.
  const visibleLines = lines.slice(0, maxLabelLines);
  const last = visibleLines.length - 1;
  const keep = charsThatFit(visibleLines[last], maxLineWidth - glyphWidth("…"));
  visibleLines[last] = `${visibleLines[last].slice(0, keep)}…`;
  return visibleLines;
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
  legend,
  overlayLeft,
  hoverCard,
  ariaLabel,
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
  /** View-specific status / size legend (bottom-right); omitted → no panel. */
  legend?: ReactNode;
  /** Extra control rendered beside the layout gear (e.g. the window selector). */
  overlayLeft?: ReactNode;
  /** Instant hover detail card; see HoverCard. Absent → native <title> tooltip. */
  hoverCard?: HoverCard;
  ariaLabel?: string;
}) {
  const t = useTranslations("fleet.forceGraph");
  const graphAriaLabel = ariaLabel ?? t("defaultAriaLabel");
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

  // Hover anchor: the node id plus the node's on-screen box captured at
  // mouseenter. The card is pinned there for the whole hover — it never
  // follows the cursor (user ruling 2026-08-29) — and clears on mouseleave.
  // x/y anchor the card beside the node's top-right corner; flipX is the
  // node's LEFT edge — the flip baseline (flipping off the right edge would
  // park the card on top of the hovered node, QA #990); h is the node box
  // height (the vertical fallback's below-placement anchor).
  const [hovered, setHovered] = useState<{ id: number; x: number; y: number; flipX: number; h: number } | null>(null);

  // Cap-state interactivity: when the card's height is capped it becomes
  // scrollable, and for the scroll to be reachable the card must accept the
  // pointer (pointer-events-auto) and survive the pointer crossing the gap
  // between node and card. `cardScrollable` mirrors the style the placement
  // effect writes; `hideTimerRef` is the grace period that covers the gap
  // crossing (QA #990 delta2: the capped content had no user path).
  const [cardScrollable, setCardScrollable] = useState(false);
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // The instant hover card. Content is computed only for the hovered node
  // (never per node per layout tick); when the view supplies no card the
  // native <title> fallback stays in place per node.
  const hoveredCard = useMemo(() => {
    if (!hovered || !hoverCard) return null;
    const node = nodeById.get(hovered.id);
    return node ? hoverCard(node) : null;
  }, [hovered, hoverCard, nodeById]);

  // Card placement, tried in order until one fits WITHOUT clamping (the card
  // must never cover the hovered node — QA #990): beside the node (right
  // preferred, then left, anchored at the node's box with a 14px gap), then
  // above / below the node centered on its horizontal center (narrow
  // canvases, mobile). When the card is taller than BOTH vertical sides (a
  // long card on a short canvas), it pins to the roomier side and its height
  // is capped so it still clears the node — the content scrolls. Done in a
  // layout effect with direct style writes — before paint, and never re-run
  // while the cursor moves (the anchor is static).
  const containerRef = useRef<HTMLDivElement | null>(null);
  const hoverCardRef = useRef<HTMLDivElement | null>(null);
  // Cancel a pending grace hide if the whole graph unmounts mid-hover.
  useEffect(() => () => {
    if (hideTimerRef.current) clearTimeout(hideTimerRef.current);
  }, []);

  useLayoutEffect(() => {
    if (!hovered) {
      // No hover — cancel any pending grace hide and clear the scroll flag
      // so a fresh hover starts from the non-scrollable state.
      if (hideTimerRef.current) {
        clearTimeout(hideTimerRef.current);
        hideTimerRef.current = null;
      }
      setCardScrollable(false);
      return;
    }
    const card = hoverCardRef.current;
    const container = containerRef.current;
    if (!card || !container) return;
    const rect = container.getBoundingClientRect();
    const cardW = card.offsetWidth;
    const cardH = card.offsetHeight;
    const gap = 14;
    const maxX = rect.width - 4;
    const maxY = rect.height - 4;
    const x = hovered.x - rect.left; // node's right edge
    const y = hovered.y - rect.top; // node's top edge
    const nodeLeft = hovered.flipX - rect.left;
    const nodeBottom = y + hovered.h;

    // A previous vertical placement may have capped the card's height — clear
    // it so this run measures the uncapped card.
    card.style.maxHeight = "";
    card.style.overflowY = "";
    setCardScrollable(false);

    let left: number;
    let top: number;
    const fitsRight = x + gap + cardW <= maxX;
    // Horizontal flip: anchor the card's RIGHT edge at the node's LEFT edge —
    // the card ends up entirely on the node's other side.
    const fitsLeft = nodeLeft - gap - cardW >= 4;
    if (fitsRight || fitsLeft) {
      left = fitsRight ? x + gap : nodeLeft - gap - cardW;
      top = y + gap;
      if (top + cardH > maxY) top = y - gap - cardH; // vertical flip beside
    } else {
      // Neither horizontal side fits (mid-band node on a narrow canvas):
      // place the card above the node, else below, horizontally centered on
      // the node (clamped into the canvas).
      left = Math.max(4, Math.min((nodeLeft + x) / 2 - cardW / 2, maxX - cardW));
      const fitsAbove = y - gap - cardH >= 4;
      const fitsBelow = nodeBottom + gap + cardH <= maxY;
      if (fitsAbove || fitsBelow) {
        top = fitsAbove ? y - gap - cardH : nodeBottom + gap;
      } else {
        // Taller than the free vertical space: pin to the roomier side and
        // cap the card's height there so it clears the node; the card body
        // scrolls (style set below).
        const aboveSpace = y - gap - 4;
        const belowSpace = maxY - nodeBottom - gap;
        if (aboveSpace >= belowSpace) {
          top = 4;
          card.style.maxHeight = `${aboveSpace}px`;
        } else {
          top = nodeBottom + gap;
          card.style.maxHeight = `${belowSpace}px`;
        }
        card.style.overflowY = "auto";
        setCardScrollable(true);
      }
    }
    card.style.left = `${Math.max(4, left)}px`;
    card.style.top = `${Math.max(4, top)}px`;
  }, [hovered]);

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

      // Scale to fit without an upper cap; retain only the positive zoom floor.
      const { cx, cy, w, h } = layoutRef.current;
      const fitScale = Math.min(w / boxW, h / boxH) * params.zoomFitRatio;
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
    <div ref={containerRef} className={cn("relative h-full w-full", OVERFLOW_HIDDEN)}>
      {layout ? (
      <svg
        ref={attachZoom}
        className="h-full w-full select-none touch-none"
        viewBox={`${minX} ${minY} ${w} ${h}`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={graphAriaLabel}
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
              const isGhost = n.ghost === true;
              const fill = statusText[n.status] ?? "text-slate-400";
              const showLabel = transform.k >= LABEL_MIN_ZOOM;
              const labelLines = showLabel && n.label ? wrapLabel(n.label, r) : [];
              const totalTextLines = 1 + labelLines.length;
              const firstTextLineY = -((totalTextLines - 1) * LABEL_LINE_HEIGHT) / 2;
              return (
                <g
                  key={n.id}
                  transform={`translate(${p.x},${p.y})`}
                  // Ghost nodes (structurally required hidden parents) render
                  // dimmed until hovered, so the tree stays readable while the
                  // real nodes keep full contrast.
                  className={cn("cursor-pointer", isGhost && !isHovered && "opacity-40")}
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
                  onMouseEnter={(ev) => {
                    // A fresh hover cancels a pending grace hide from the
                    // previous node.
                    if (hideTimerRef.current) {
                      clearTimeout(hideTimerRef.current);
                      hideTimerRef.current = null;
                    }
                    // Static anchor: the card is pinned to the node's own
                    // on-screen box and stays put until mouseleave — no
                    // cursor chasing. x/y = the top-right corner (normal
                    // side); flipX = the left edge (flip baseline).
                    const box = ev.currentTarget.getBoundingClientRect();
                    setHovered({ id: n.id, x: box.right, y: box.top, flipX: box.left, h: box.height });
                  }}
                  onMouseLeave={(ev) => {
                    const card = hoverCardRef.current;
                    // Cap state: the card is interactive (scrollable). If
                    // the pointer landed inside it, the hover stays;
                    // otherwise a short grace period covers the pointer
                    // crossing the gap between node and card (QA #990
                    // delta2: mouseleave unmounted the card before the
                    // scrollable content could be reached).
                    if (cardScrollable && card && ev.relatedTarget instanceof Node && card.contains(ev.relatedTarget)) {
                      return;
                    }
                    if (cardScrollable) {
                      if (hideTimerRef.current) clearTimeout(hideTimerRef.current);
                      hideTimerRef.current = setTimeout(
                        () => setHovered((cur) => (cur?.id === n.id ? null : cur)),
                        200,
                      );
                      return;
                    }
                    setHovered((cur) => (cur?.id === n.id ? null : cur));
                  }}
                >
                  {hoverCard == null ? (
                    <title>{n.nodeTitle ?? (n.label ? `${n.label} (#${n.id})` : `#${n.id}`)}</title>
                  ) : null}
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
                      strokeDasharray={isGhost ? "3 2" : undefined}
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
                      strokeDasharray={isGhost ? "3 2" : undefined}
                      opacity={isHovered ? 1 : 0.92}
                    />
                  )}
                  {showLabel ? (
                    <text
                      textAnchor="middle"
                      dominantBaseline="central"
                      className="fill-white font-mono text-2xs font-semibold"
                      stroke="rgba(0,0,0,0.35)"
                      strokeWidth={0.75}
                      paintOrder="stroke"
                      style={{ pointerEvents: "none" }}
                    >
                      <tspan x={0} y={firstTextLineY} dominantBaseline="central">
                        #{n.id}
                      </tspan>
                      {labelLines.map((line, index) => (
                        <tspan key={index} x={0} dy={LABEL_LINE_HEIGHT} dominantBaseline="central">
                          {line}
                        </tspan>
                      ))}
                    </text>
                  ) : null}
                </g>
              );
            })}
          </g>
        </g>
      </svg>
      ) : nodes.length > 0 ? (
        <div className={cn("h-full items-center justify-center text-xs text-muted-foreground", FLEX)}>
          {t("loading")}
        </div>
      ) : null}

      {/* Stats bar: node / edge counts. */}
      {statsText != null ? (
        <div className="pointer-events-none absolute bottom-3 left-3 rounded-md border border-border bg-background/80 px-3 py-1.5 font-mono text-2xs text-muted-foreground tabular-nums backdrop-blur">
          {statsText}
        </div>
      ) : null}

      {legend != null ? (
        <div className="pointer-events-none absolute bottom-3 right-3 rounded-md border border-border bg-background/80 px-3 py-1.5 text-2xs text-muted-foreground backdrop-blur">
          {legend}
        </div>
      ) : null}

      {/* Instant hover detail card — pointer-events-none in the normal
          states so it never steals the cursor from the nodes beneath; in the
          height-capped state the card body scrolls, so the card flips to
          pointer-events-auto to stay reachable (its mouseenter cancels the
          pending grace hide, its mouseleave unmounts). Position is set by
          the layout effect above (flip-at-edge, before paint). */}
      {hoveredCard != null ? (
        <div
          ref={hoverCardRef}
          role="tooltip"
          className={cn("absolute z-20", cardScrollable ? "pointer-events-auto" : "pointer-events-none")}
          style={{ left: 0, top: 0 }}
          onMouseEnter={() => {
            // The pointer reached the card across the gap — cancel the
            // pending grace hide so the card stays while it is scrolled.
            if (hideTimerRef.current) {
              clearTimeout(hideTimerRef.current);
              hideTimerRef.current = null;
            }
          }}
          onMouseLeave={() => {
            // Leaving the card hides it (re-entering the node re-anchors on
            // the node's own mouseenter).
            if (hideTimerRef.current) {
              clearTimeout(hideTimerRef.current);
              hideTimerRef.current = null;
            }
            setHovered(null);
          }}
        >
          {hoveredCard}
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
          className={cn("size-6 items-center justify-center rounded border border-border bg-background/80 text-2xs text-muted-foreground backdrop-blur hover:bg-sidebar-accent hover:text-foreground", FLEX)}
          aria-label={t("resetZoom")}
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
