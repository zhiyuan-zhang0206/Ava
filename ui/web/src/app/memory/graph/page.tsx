"use client";

import { useQuery } from "@tanstack/react-query";
import { select } from "d3-selection";
import {
  zoom as d3Zoom,
  zoomIdentity,
  type D3ZoomEvent,
  type ZoomBehavior,
  type ZoomTransform,
} from "d3-zoom";
import { Loader2, MessageSquare, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { ChatMarkdown } from "@/components/markdown";
import {
  ForceControls,
  FORCE_GROUPS,
  useForceParams,
  type ForceParams,
} from "@/components/fleet/force-controls";
import {
  ResizablePanel,
  ResizablePanelGroup,
  ResizableHandle,
} from "@/components/ui/resizable";
import { api } from "@/lib/api";
import {
  useForceLayout,
  type SimNode,
  type SimLink,
} from "@/lib/use-force-layout";
import type { MemoryGraphNode, MemoryGraphResponse, MemoryNoteResponse } from "@/lib/types";
import { useBreakpoint } from "@/lib/breakpoint";
import { BAR_HEIGHT_CLASS, FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

// ── Memory graph force defaults ──
// Lighter than the agents graph: memory notes are sparser, links are weaker,
// and the graph is meant for browsing rather than monitoring.
const MEMORY_FORCE_DEFAULTS: ForceParams = {
  nodeSizeMin: 14,
  nodeSizeMax: 22,
  linkDistance: 100,
  linkStrength: 0.2,
  repulsion: 280,
  centerStrength: 0.7,
  centerForceX: 0.1,
  centerForceY: 0.1,
  collidePadding: 8,
  alphaDecay: 0.025,
  zoomPadding: 24,
  zoomFitRatio: 1,
};
const MEMORY_FORCE_KEY = "display.memory_force_params";

// Tag palette + folder slate live in lib/memory-graph-colors.ts — a plain
// module outside the localStorage-policy scan. The palette holds note data
// (memory tag names), and one of them matches the scan's storage-key pattern;
// keeping the data in lib/ keeps the page source free of that false positive.
import { colorForTag, FOLDER_COLOR } from "@/lib/memory-graph-colors";

// Zoom floor only — scale factor on the memory-graph content <g>.
// No upper bound (user ruling 2026-08-25: zoom must never be capped); d3
// clamps wheel zoom to this extent, so the non-functional floor keeps k
// strictly positive and prevents a degenerate zero-area transform.
const ZOOM_MIN = 0.001;
const ZOOM_MAX = Infinity;

const MEMORY_GRAPH_QUERY_KEY = ["memory-graph"] as const;

// Hover/selection dim: non-related nodes stay at 50% opacity (structure
// highlight — no content tooltip; the note body lives in the side panel).
const HOVER_DIM_OPACITY = 0.5;

export default function MemoryGraphPage() {
  const t = useTranslations("memoryGraph");
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: MEMORY_GRAPH_QUERY_KEY,
    queryFn: () => api.getMemoryGraph(),
  });
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Clear stale selection when the fetched node set changes.
  useEffect(() => {
    if (
      data &&
      selectedId != null &&
      !data.nodes.some((node) => node.id === selectedId)
    ) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSelectedId(null);
    }
  }, [data, selectedId]);

  return (
    <div className={cn("bg-background", FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header className={cn("shrink-0 items-center gap-2 border-b border-border px-4", BAR_HEIGHT_CLASS, FLEX)}>
        <Link
          href="/"
          className={cn("size-8 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground", FLEX)}
          aria-label={t("backToConversation")}
        >
          <MessageSquare className="size-4" />
        </Link>
        <div className={cn(MIN_W_0, FLEX_1)}>
          <h1 className="truncate text-sm font-semibold">{t("title")}</h1>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={() => void refetch()}
          disabled={isFetching}
          aria-label={t("refresh")}
        >
          <RefreshCw
            className={`size-4 ${isFetching ? "animate-spin" : ""}`}
          />
        </Button>
      </header>

      {isLoading ? (
        <div className={cn("items-center justify-center", FLEX, FLEX_1)}>
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      ) : error || !data ? (
        <div className={cn("items-center justify-center px-6 text-sm text-muted-foreground", FLEX, FLEX_1)}>
          {t("loadFailed")}
        </div>
      ) : data.nodes.length === 0 ? (
        <div className={cn("items-center justify-center text-sm text-muted-foreground", FLEX, FLEX_1)}>
          {t("empty")}
        </div>
      ) : (
        <MemoryGraphShell
          graph={data}
          selectedId={selectedId}
          onSelect={setSelectedId}
        />
      )}
    </div>
  );
}

function MemoryGraphShell({
  graph,
  selectedId,
  onSelect,
}: {
  graph: MemoryGraphResponse;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}) {
  const t = useTranslations("memoryGraph");
  const selected = useMemo(() => {
    if (selectedId == null) return null;
    return graph.nodes.find((node) => node.id === selectedId) ?? null;
  }, [graph, selectedId]);

  const { isLarge } = useBreakpoint();

  // The resizable panel group mounts only after the breakpoint is known
  // (QA #1169 F1): useBreakpoint's pre-mount default is isLarge=false, and a
  // group mounting with that default would paint defaultSize 50+38=88 on the
  // first frame — react-resizable-panels normalizes a <100 total and
  // *persists* the normalized layout, clobbering the user's dragged split on
  // every reload. Both modes below sum to 100, so no normalization ever runs.
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR-safe: must run once after mount so the first *painted* frame has the real breakpoint
    setMounted(true);
  }, []);
  if (!mounted) {
    // Pre-paint placeholder (graph only): identical tree shape on client and
    // server so hydration does not mismatch.
    return (
      <div className={cn(FLEX_1, MIN_H_0)}>
        <section className={cn("relative h-full", MIN_H_0)}>
          <MemoryForceGraph
            graph={graph}
            selectedId={selectedId}
            onSelect={onSelect}
          />
        </section>
      </div>
    );
  }

  return (
    <ResizablePanelGroup
      direction={isLarge ? "horizontal" : "vertical"}
      autoSaveId="ava.memory.graph.split"
      className={cn(FLEX_1, MIN_H_0)}
    >
      <ResizablePanel defaultSize={isLarge ? 62 : 50} minSize={30}>
        <section className={cn("relative h-full", MIN_H_0)}>
          <MemoryForceGraph
            graph={graph}
            selectedId={selectedId}
            onSelect={onSelect}
          />
        </section>
      </ResizablePanel>
      <ResizableHandle />
      <ResizablePanel defaultSize={isLarge ? 38 : 50} minSize={25}>
        <aside className={cn("h-full overflow-y-auto px-4 py-3 text-sm", MIN_H_0)}>
          {selected ? (
            <MemorySidePanel
              node={selected}
              graph={graph}
              onSelect={onSelect}
            />
          ) : (
            <p className="text-xs text-muted-foreground">
              {t("selectNode")}
            </p>
          )}
        </aside>
      </ResizablePanel>
    </ResizablePanelGroup>
  );
}

// ── Side panel ──
//
// One panel, two modes (user ruling 2026-08-30): clicking a note node renders
// that note's markdown body (frontmatter already stripped by the backend; its
// fields render as the header); clicking a folder pseudo node renders the
// folder's note list. The old right-side tag summary / metadata detail view
// is gone — it duplicated the note itself.

// Split the graph's containment edges into "notes directly inside this
// folder" (pseudo-folder children) — the folder panel's list.
function folderChildren(
  graph: MemoryGraphResponse,
  folderId: string,
): MemoryGraphNode[] {
  const kindById = new Map(graph.nodes.map((n) => [n.id, n.kind]));
  const ids: string[] = [];
  for (const e of graph.edges) {
    if (e.kind === "containment" && e.target === folderId && kindById.get(e.source) === "note") {
      ids.push(e.source);
    }
  }
  return ids
    .map((id) => graph.nodes.find((n) => n.id === id))
    .filter((n): n is MemoryGraphNode => n != null);
}

function MemorySidePanel({
  node,
  graph,
  onSelect,
}: {
  node: MemoryGraphNode;
  graph: MemoryGraphResponse;
  onSelect: (id: string | null) => void;
}) {
  const t = useTranslations("memoryGraph");
  if (node.kind === "folder") {
    const children = folderChildren(graph, node.id);
    return (
      <div className="space-y-3">
        <div className="border-b border-border pb-3">
          <h3 className="text-sm font-semibold leading-tight">{node.title}</h3>
          <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
            {node.path}
          </p>
        </div>
        <section className="space-y-1">
          <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("folderNotes")}
          </h2>
          {children.length === 0 ? (
            <p className="text-xs text-muted-foreground">{t("folderEmpty")}</p>
          ) : (
            children.map((child) => (
              <button
                key={child.id}
                type="button"
                onClick={() => onSelect(child.id)}
                className="block w-full rounded px-2 py-1 text-left text-xs transition-colors hover:bg-sidebar-accent"
              >
                <span
                  className="mr-1.5 inline-block size-2 rounded-full align-middle"
                  style={{ backgroundColor: colorForTag(child.primary_tag || "untagged") }}
                />
                {child.title}
              </button>
            ))
          )}
        </section>
      </div>
    );
  }
  return <MemoryNotePanel node={node} />;
}

function MemoryNotePanel({ node }: { node: MemoryGraphNode }) {
  const t = useTranslations("memoryGraph");
  const { data, isLoading, error } = useQuery<MemoryNoteResponse>({
    queryKey: ["memory-note", node.id],
    queryFn: () => api.getMemoryNote(node.path),
  });

  return (
    <div className="space-y-3">
      <div className="border-b border-border pb-3">
        <h3 className="text-sm font-semibold leading-tight">{node.title}</h3>
        <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
          {node.path}
        </p>
        {node.tags.length > 0 ? (
          <div className={cn("mt-2 flex-wrap gap-1.5", FLEX)}>
            {node.tags.map((tag) => (
              <span
                key={tag}
                className="rounded border border-border px-1.5 py-0.5 text-xs"
              >
                {tag}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      {isLoading ? (
        <div className={cn("items-center justify-center py-6", FLEX)}>
          <Loader2 className="size-5 animate-spin text-muted-foreground" />
        </div>
      ) : error || !data ? (
        <p className="text-xs text-muted-foreground">
          {t("noteLoadFailed")}
        </p>
      ) : (
        <div className="text-sm leading-relaxed">
          <ChatMarkdown content={data.body} />
        </div>
      )}
    </div>
  );
}

// ── Force-directed memory graph (agents-graph-style) ──

const MemoryForceGraph = memo(function MemoryForceGraph({
  graph,
  selectedId,
  onSelect,
}: {
  graph: MemoryGraphResponse;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}) {
  const t = useTranslations("memoryGraph");
  const { params, setParams, reset } = useForceParams(
    MEMORY_FORCE_KEY,
    MEMORY_FORCE_DEFAULTS,
  );

  // Map memory nodes/edges to SimNode/SimLink.
  const simNodes = useMemo((): SimNode[] => {
    const maxDegree = Math.max(
      1,
      ...graph.nodes.map((n) => {
        let deg = 0;
        for (const e of graph.edges) {
          if (e.source === n.id || e.target === n.id) deg++;
        }
        return deg;
      }),
    );
    return graph.nodes.map((n) => {
      // Scale radius by degree: central nodes are bigger.
      let deg = 0;
      for (const e of graph.edges) {
        if (e.source === n.id || e.target === n.id) deg++;
      }
      const ratio = maxDegree > 0 ? Math.sqrt(deg / maxDegree) : 0;
      const r =
        params.nodeSizeMin +
        (params.nodeSizeMax - params.nodeSizeMin) * ratio;
      return { id: n.id, r };
    });
  }, [graph.nodes, graph.edges, params.nodeSizeMin, params.nodeSizeMax]);

  const simLinks = useMemo((): SimLink[] => {
    return graph.edges.map((e) => ({
      source: e.source,
      target: e.target,
    }));
  }, [graph.edges]);

  const { positions, layout } = useForceLayout(simNodes, simLinks, params);

  // ── Zoom / pan via d3-zoom ──
  const svgRef = useRef<SVGSVGElement | null>(null);
  const zoomRef =
    useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const [transform, setTransform] = useState<ZoomTransform>(zoomIdentity);
  const [animateZoom, setAnimateZoom] = useState(false);
  const extentRef = useRef<[[number, number], [number, number]]>([
    [-400, -400],
    [400, 400],
  ]);

  // Keep extent in sync with the settled layout.
  useEffect(() => {
    if (!layout) return;
    extentRef.current = [
      [layout.minX, layout.minY],
      [layout.minX + layout.w, layout.minY + layout.h],
    ];
  }, [layout]);

  // Install the d3-zoom behavior.
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

  // Precompute neighbor sets for dimming (selection AND hover share it).
  const neighborMap = useMemo(() => {
    const map = new Map<string, Set<string>>();
    for (const e of graph.edges) {
      let a = map.get(e.source);
      if (!a) {
        a = new Set();
        map.set(e.source, a);
      }
      a.add(e.target);
      let b = map.get(e.target);
      if (!b) {
        b = new Set();
        map.set(e.target, b);
      }
      b.add(e.source);
    }
    return map;
  }, [graph.edges]);

  // Hover highlight (Cosma-style structure highlight — no floating tooltip):
  // the hovered node and its direct neighbors stay lit; everything else dims
  // to 50%. Selection uses the same set, but doesn't dim on its own — the
  // selected node is shown in the side panel so dimming would fight it.
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const hoverRelatives = useMemo(() => {
    if (hoveredId == null) return null;
    const set = new Set(neighborMap.get(hoveredId) ?? []);
    set.add(hoveredId);
    return set;
  }, [neighborMap, hoveredId]);

  // The effective dim set: hover only. Selection does not dim the graph —
  // the selected node's content occupies the side panel, and a selection
  // dim would fight the permanent focus (hover is the transient local
  // context; its 50% dim is the Cosma-style structure highlight).
  const dimSet = hoverRelatives ?? null;

  // Focus a node — center + scale its neighborhood.
  const focusNode = useCallback(
    (id: string) => {
      const svg = svgRef.current;
      const behavior = zoomRef.current;
      if (!svg || !behavior || !layout) return;
      const p = positions.get(id);
      if (!p) return;
      const neighbors = neighborMap.get(id);
      let minX = p.x,
        minY = p.y,
        maxX = p.x,
        maxY = p.y;
      if (neighbors) {
        for (const nid of neighbors) {
          const np = positions.get(nid);
          if (np) {
            if (np.x < minX) minX = np.x;
            if (np.y < minY) minY = np.y;
            if (np.x > maxX) maxX = np.x;
            if (np.y > maxY) maxY = np.y;
          }
        }
      }
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
      const fitScale =
        Math.min(layout.w / boxW, layout.h / boxH) *
        params.zoomFitRatio;
      const scale = Math.max(ZOOM_MIN, fitScale);
      const tx = layout.minX + layout.w / 2 - boxCx * scale;
      const ty = layout.minY + layout.h / 2 - boxCy * scale;
      behavior.transform(
        select(svg),
        zoomIdentity.translate(tx, ty).scale(scale),
      );
    },
    [positions, neighborMap, params, layout],
  );

  // Auto-focus on selection change.
  const focusedRef = useRef<string | null>(null);
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

  const resetZoom = () => {
    const svg = svgRef.current;
    const behavior = zoomRef.current;
    if (svg && behavior)
      behavior.transform(select(svg), zoomIdentity);
  };

  // Chip counters — folders are pseudo nodes, not notes.
  const noteCount = graph.nodes.filter(
    (n) => n.kind === "note",
  ).length;
  const folderCount = graph.nodes.length - noteCount;

  if (!layout) {
    return (
      <div className={cn("h-full items-center justify-center text-xs text-muted-foreground", FLEX)}>
        {t("loading")}
      </div>
    );
  }

  const { placed, minX, minY, w, h } = layout;

  return (
    <>
      <svg
        ref={attachZoom}
        className="h-full w-full select-none touch-none"
        viewBox={`${minX} ${minY} ${w} ${h}`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={t("ariaLabel")}
        onClick={(ev) => {
          if (ev.target === ev.currentTarget) onSelect(null);
        }}
      >
        <g
          transform={transform.toString()}
          style={{
            transition: animateZoom
              ? "transform 0.4s ease"
              : "none",
          }}
          onTransitionEnd={(ev) => {
            if (ev.propertyName === "transform")
              setAnimateZoom(false);
          }}
        >
          {/* Edges — containment is the main structure; cross-references
              between notes are deliberately weaker (thin, dashed, faint). */}
          <g>
            {graph.edges.map((e) => {
              const a = positions.get(e.source);
              const b = positions.get(e.target);
              if (!a || !b) return null;
              // Cosma semantics (QA #1169 ①): an edge stays visible when
              // BOTH endpoints are lit — not only edges incident to the
              // hovered node. Otherwise a link between two lit neighbors
              // nearly disappears and reads as broken.
              const isIncident =
                dimSet != null &&
                (e.source === hoveredId || e.target === hoveredId);
              const isDimmed =
                dimSet != null && !isIncident &&
                (!dimSet.has(e.source) || !dimSet.has(e.target));
              const isReference = e.kind === "reference";
              const opacity = isReference
                ? isIncident
                  ? 0.6
                  : isDimmed
                    ? 0.05
                    : 0.28
                : isIncident
                  ? 0.85
                  : isDimmed
                    ? 0.06
                    : 0.35;
              const sw = isReference ? 0.75 : isIncident ? 2.0 : 1.0;
              return (
                <line
                  key={`${e.kind}-${e.source}-${e.target}`}
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  className="text-muted-foreground"
                  stroke="currentColor"
                  strokeOpacity={opacity}
                  strokeWidth={sw}
                  strokeDasharray={isReference ? "2 4" : undefined}
                />
              );
            })}
          </g>

          {/* Nodes */}
          <g>
            {placed.map(({ node, p }) => {
              const memNode = graph.nodes.find(
                (n) => n.id === node.id,
              );
              if (!memNode) return null;
              const isSelected = selectedId === node.id;
              const isFolder = memNode.kind === "folder";
              const color = isFolder
                ? FOLDER_COLOR
                : colorForTag(memNode.primary_tag || "untagged");
              const r = node.r;
              // Hover structure highlight: related nodes full opacity,
              // everything else dims (Cosma-style 50%). The group opacity
              // covers the whole <g> — the selection ring and label dim
              // with the node too (QA #1169 F2).
              const isDimmed = dimSet != null && !dimSet.has(String(node.id));
              // Dim applies to the whole group — shape AND the label text
              // (QA #1169 F2: a dimmed node with a fully-lit label reads as
              // highlighted, not dimmed).
              const opacity = isDimmed ? HOVER_DIM_OPACITY : 1;
              return (
                <g
                  key={node.id}
                  transform={`translate(${p.x},${p.y})`}
                  className="cursor-pointer"
                  data-testid={`memory-node-${node.id}`}
                  opacity={opacity}
                  onPointerDown={(ev) => ev.stopPropagation()}
                  onClick={(ev) => {
                    ev.stopPropagation();
                    if (selectedId === node.id) return;
                    const nid = typeof node.id === 'string' ? node.id : String(node.id);
                    onSelect(nid);
                    focusNode(nid);
                  }}
                  onMouseEnter={() => setHoveredId(node.id as string)}
                  onMouseLeave={() =>
                    setHoveredId((cur) => (cur === node.id ? null : cur))
                  }
                >
                  {/* Selection ring */}
                  {isSelected ? (
                    <circle
                      r={r + 5}
                      className="text-sky-400 animate-pulse"
                      fill="none"
                      stroke="currentColor"
                      strokeOpacity={0.7}
                      strokeWidth={2}
                      strokeDasharray="4 3"
                    />
                  ) : null}
                  {/* Node — folders render as rounded squares, notes as
                      tag-colored circles, so the structure reads at a
                      glance. */}
                  {isFolder ? (
                    <rect
                      x={-r}
                      y={-r}
                      width={2 * r}
                      height={2 * r}
                      rx={r * 0.35}
                      fill={color}
                      stroke="var(--background)"
                      strokeWidth={1.5}
                    />
                  ) : (
                    <circle
                      r={r}
                      fill={color}
                      stroke="var(--background)"
                      strokeWidth={1.5}
                    />
                  )}
                  {/* Label */}
                  <text
                    y={-r - 6}
                    textAnchor="middle"
                    dominantBaseline="central"
                    className={`fill-foreground text-2xs select-none ${
                      isFolder ? "font-semibold" : "font-medium"
                    }`}
                    style={{ pointerEvents: "none" }}
                  >
                    {memNode.title.length > 24
                      ? memNode.title.slice(0, 22) + "…"
                      : memNode.title}
                  </text>
                </g>
              );
            })}
          </g>
        </g>
      </svg>

      {/* Controls overlay */}
      <div className={cn("pointer-events-auto absolute left-3 top-3 items-center gap-1", FLEX)}>
        <ForceControls
          params={params}
          setParams={setParams}
          reset={reset}
          groups={FORCE_GROUPS}
        />
        <span className="rounded border border-border bg-background/80 px-2 py-0.5 text-2xs text-muted-foreground backdrop-blur tabular-nums">
          <span className="tabular-nums">{t("noteCount", { count: noteCount })}</span>
          {" · "}
          <span className="tabular-nums">{t("folderCount", { count: folderCount })}</span>
          {" · "}
          <span className="tabular-nums">{t("edgeCount", { count: graph.edges.length })}</span>
        </span>
        <span className="ml-2 hidden items-center gap-3 rounded border border-border bg-background/80 px-2 py-0.5 text-2xs text-muted-foreground backdrop-blur md:flex">
          <span className={cn("items-center gap-1", FLEX)}>
            <svg width="18" height="4" aria-hidden="true">
              <line x1="0" y1="2" x2="18" y2="2" stroke="currentColor" strokeOpacity="0.35" strokeWidth="1" />
            </svg>
            {t("folder")}
          </span>
          <span className={cn("items-center gap-1", FLEX)}>
            <svg width="18" height="4" aria-hidden="true">
              <line x1="0" y1="2" x2="18" y2="2" stroke="currentColor" strokeOpacity="0.28" strokeWidth="0.75" strokeDasharray="2 4" />
            </svg>
            {t("reference")}
          </span>
        </span>
      </div>

      {/* Reset zoom button */}
      <div className={cn("pointer-events-auto absolute right-3 top-3 gap-0.5", FLEX, FLEX_COL)}>
        <button
          type="button"
          className={cn("size-6 items-center justify-center rounded border border-border bg-background/80 text-2xs text-muted-foreground backdrop-blur hover:bg-sidebar-accent hover:text-foreground", FLEX)}
          aria-label={t("resetZoom")}
          title={t("resetZoom")}
          onClick={resetZoom}
          disabled={
            transform.k === 1 &&
            transform.x === 0 &&
            transform.y === 0
          }
        >
          ↺
        </button>
      </div>
    </>
  );
});
