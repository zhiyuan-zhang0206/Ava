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
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  ForceControls,
  FORCE_GROUPS,
  useForceParams,
  type ForceParams,
} from "@/components/fleet/force-controls";
import { api } from "@/lib/api";
import {
  useForceLayout,
  type SimNode,
  type SimLink,
} from "@/lib/use-force-layout";
import type { MemoryGraphNode, MemoryGraphResponse } from "@/lib/types";
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

// ── Tag color palette ──
const TAG_COLORS: Record<string, string> = {
  "user-profile": "#0284c7",
  "ava-internal": "#475569",
  intelligence: "#7c3aed",
  "org-collab": "#db2777",
  "career-strategy": "#d97706",
  "life-log": "#16a34a",
  "tech-ops": "#0891b2",
  hobby: "#65a30d",
  health: "#dc2626",
  infra: "#6366f1",
  personal: "#f59e0b",
  projects: "#14b8a6",
  agents: "#8b5cf6",
  untagged: "#737373",
};

function colorForTag(tag: string): string {
  return TAG_COLORS[tag] ?? TAG_COLORS.untagged;
}

// Folder pseudo nodes share one neutral slate so the structure reads as
// backdrop and note tag colors stay the only color signal.
const FOLDER_COLOR = "#64748b";

// Zoom floor only — scale factor on the memory-graph content <g>.
// No upper bound (user ruling 2026-08-25: zoom must never be capped); d3
// clamps wheel zoom to this extent, so the non-functional floor keeps k
// strictly positive and prevents a degenerate zero-area transform.
const ZOOM_MIN = 0.001;
const ZOOM_MAX = Infinity;

const MEMORY_GRAPH_QUERY_KEY = ["memory-graph"] as const;

export default function MemoryGraphPage() {
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
          aria-label="Back to conversation"
        >
          <MessageSquare className="size-4" />
        </Link>
        <div className={cn(MIN_W_0, FLEX_1)}>
          <h1 className="truncate text-sm font-semibold">Memory graph</h1>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={() => void refetch()}
          disabled={isFetching}
          aria-label="Refresh memory graph"
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
          Couldn&apos;t load memory graph.
        </div>
      ) : data.nodes.length === 0 ? (
        <div className={cn("items-center justify-center text-sm text-muted-foreground", FLEX, FLEX_1)}>
          No memory notes to graph.
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
  // Tag summary for the sidebar — notes only, folder pseudo nodes carry
  // no tag.
  const tags = useMemo(() => {
    const counts = new Map<string, number>();
    for (const node of graph.nodes) {
      if (node.kind === "folder") continue;
      counts.set(
        node.primary_tag || "untagged",
        (counts.get(node.primary_tag || "untagged") ?? 0) + 1,
      );
    }
    return [...counts.entries()].sort(
      (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
    );
  }, [graph.nodes]);

  // Notes per folder pseudo node (containment edges whose source is a note).
  const folderNoteCounts = useMemo(() => {
    const kindById = new Map(graph.nodes.map((n) => [n.id, n.kind]));
    const counts = new Map<string, number>();
    for (const e of graph.edges) {
      if (e.kind === "containment" && kindById.get(e.source) === "note") {
        counts.set(e.target, (counts.get(e.target) ?? 0) + 1);
      }
    }
    return counts;
  }, [graph.nodes, graph.edges]);

  const selected = useMemo(() => {
    if (selectedId == null) return null;
    return graph.nodes.find((node) => node.id === selectedId) ?? null;
  }, [graph, selectedId]);

  return (
    <main className={cn("grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_18rem]", FLEX_1, MIN_H_0)}>
      <section className={cn("relative border-b border-border lg:border-b-0 lg:border-r", MIN_H_0)}>
        <MemoryForceGraph
          graph={graph}
          selectedId={selectedId}
          onSelect={onSelect}
          folderNoteCounts={folderNoteCounts}
        />
      </section>
      <aside className={cn("overflow-y-auto px-4 py-3 text-sm", MIN_H_0)}>
        <section className="space-y-2 border-b border-border pb-4">
          <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Tags
          </h2>
          <div className={cn("flex-wrap gap-1.5", FLEX)}>
            {tags.map(([tag, count]) => (
              <span
                key={tag}
                className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-1 text-xs"
              >
                <span
                  className="size-2 rounded-full"
                  style={{ backgroundColor: colorForTag(tag) }}
                />
                <span>{tag}</span>
                <span className="font-mono text-muted-foreground">
                  {count}
                </span>
              </span>
            ))}
          </div>
        </section>
        <section className="space-y-3 py-4">
          <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Selection
          </h2>
          {selected ? (
            <MemoryDetails
              node={selected}
              noteCount={folderNoteCounts.get(selected.id)}
            />
          ) : (
            <p className="text-xs text-muted-foreground">
              Click a node to see details.
            </p>
          )}
        </section>
      </aside>
    </main>
  );
}

// ── Force-directed memory graph (agents-graph-style) ──

const MemoryForceGraph = memo(function MemoryForceGraph({
  graph,
  selectedId,
  onSelect,
  folderNoteCounts,
}: {
  graph: MemoryGraphResponse;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  folderNoteCounts: Map<string, number>;
}) {
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

  // Precompute neighbor sets for selection dimming.
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

  const connectedNodeIds = useMemo(() => {
    if (selectedId == null) return null;
    return neighborMap.get(selectedId) ?? new Set();
  }, [neighborMap, selectedId]);

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

  // ── Hover tooltip state ──
  const [hovered, setHovered] = useState<{
    id: string;
    x: number;
    y: number;
  } | null>(null);
  const hoverThrottleRef = useRef(0);

  const hoveredNode = hovered
    ? graph.nodes.find((n) => n.id === hovered.id) ?? null
    : null;

  // Chip counters — folders are pseudo nodes, not notes.
  const noteCount = graph.nodes.filter(
    (n) => n.kind === "note",
  ).length;
  const folderCount = graph.nodes.length - noteCount;

  if (!layout) {
    return (
      <div className={cn("h-full items-center justify-center text-xs text-muted-foreground", FLEX)}>
        Loading…
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
        aria-label="Memory note graph"
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
              const isIncident =
                connectedNodeIds != null &&
                (e.source === selectedId ||
                  e.target === selectedId);
              const isDimmed =
                connectedNodeIds != null && !isIncident;
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
              return (
                <g
                  key={node.id}
                  transform={`translate(${p.x},${p.y})`}
                  className="cursor-pointer"
                  data-testid={`memory-node-${node.id}`}
                  onPointerDown={(ev) => ev.stopPropagation()}
                  onClick={(ev) => {
                    ev.stopPropagation();
                    if (selectedId === node.id) return;
                    const nid = typeof node.id === 'string' ? node.id : String(node.id);
                    onSelect(nid);
                    focusNode(nid);
                  }}
                  onMouseEnter={(ev) =>
                    setHovered({
                      id: node.id as string,
                      x: ev.clientX,
                      y: ev.clientY,
                    })
                  }
                  onMouseMove={(ev) => {
                    const now = Date.now();
                    if (now - hoverThrottleRef.current < 50)
                      return;
                    hoverThrottleRef.current = now;
                    setHovered({
                      id: node.id as string,
                      x: ev.clientX,
                      y: ev.clientY,
                    });
                  }}
                  onMouseLeave={() =>
                    setHovered((cur) =>
                      cur?.id === node.id ? null : cur,
                    )
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
                      opacity={
                        connectedNodeIds == null ||
                        isSelected ||
                        connectedNodeIds.has(node.id)
                          ? 1
                          : 0.15
                      }
                    />
                  ) : (
                    <circle
                      r={r}
                      fill={color}
                      stroke="var(--background)"
                      strokeWidth={1.5}
                      opacity={
                        connectedNodeIds == null ||
                        isSelected ||
                        connectedNodeIds.has(node.id)
                          ? 1
                          : 0.15
                      }
                    />
                  )}
                  {/* Label */}
                  <text
                    y={-r - 6}
                    textAnchor="middle"
                    dominantBaseline="central"
                    className={`fill-foreground text-[7px] select-none ${
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
        <span className="rounded border border-border bg-background/80 px-2 py-0.5 text-[10px] text-muted-foreground backdrop-blur tabular-nums">
          <span className="tabular-nums">{noteCount} notes</span>
          {" · "}
          <span className="tabular-nums">{folderCount} folder{folderCount === 1 ? '' : 's'}</span>
          {" · "}
          <span className="tabular-nums">{graph.edges.length} edge{graph.edges.length === 1 ? '' : 's'}</span>
        </span>
        <span className="ml-2 hidden items-center gap-3 rounded border border-border bg-background/80 px-2 py-0.5 text-[10px] text-muted-foreground backdrop-blur md:flex">
          <span className={cn("items-center gap-1", FLEX)}>
            <svg width="18" height="4" aria-hidden="true">
              <line x1="0" y1="2" x2="18" y2="2" stroke="currentColor" strokeOpacity="0.35" strokeWidth="1" />
            </svg>
            folder
          </span>
          <span className={cn("items-center gap-1", FLEX)}>
            <svg width="18" height="4" aria-hidden="true">
              <line x1="0" y1="2" x2="18" y2="2" stroke="currentColor" strokeOpacity="0.28" strokeWidth="0.75" strokeDasharray="2 4" />
            </svg>
            reference
          </span>
        </span>
      </div>

      {/* Reset zoom button */}
      <div className={cn("pointer-events-auto absolute right-3 top-3 gap-0.5", FLEX, FLEX_COL)}>
        <button
          type="button"
          className={cn("size-6 items-center justify-center rounded border border-border bg-background/80 text-[10px] text-muted-foreground backdrop-blur hover:bg-sidebar-accent hover:text-foreground", FLEX)}
          aria-label="Reset zoom"
          title="Reset zoom"
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

      {/* Tooltip */}
      {hovered && hoveredNode ? (
        <MemoryTooltip
          node={hoveredNode}
          x={hovered.x}
          y={hovered.y}
          noteCount={folderNoteCounts.get(hoveredNode.id)}
        />
      ) : null}
    </>
  );
});

// ── Tooltip ──

function MemoryTooltip({
  node,
  x,
  y,
  noteCount,
}: {
  node: MemoryGraphNode;
  x: number;
  y: number;
  noteCount?: number;
}) {
  return (
    <div
      className="pointer-events-none fixed z-50 max-w-xs rounded-md border border-border bg-popover px-3 py-2 text-popover-foreground shadow-md"
      style={{ left: x + 14, top: y + 14 }}
    >
      <div className={cn("items-center gap-2", FLEX)}>
        <span
          className="size-2 rounded-full"
          style={{
            backgroundColor: colorForTag(
              node.primary_tag || "untagged",
            ),
          }}
        />
        <span className="text-xs font-semibold">{node.title}</span>
      </div>
      {node.kind === "folder" ? (
        <div className="mt-1 text-[11px] text-muted-foreground">
          {noteCount ?? 0} note{(noteCount ?? 0) === 1 ? "" : "s"}
        </div>
      ) : node.description ? (
        <div className="mt-1 text-[11px] text-muted-foreground">
          {node.description}
        </div>
      ) : null}
      <div className={cn("mt-1 gap-3 text-[10px] tabular-nums text-muted-foreground", FLEX)}>
        <span>{node.path}</span>
        {node.ava_agent ? (
          <span>Agent #{node.ava_agent}</span>
        ) : null}
      </div>
      {node.tags.length > 0 ? (
        <div className={cn("mt-1.5 flex-wrap gap-1", FLEX)}>
          {node.tags.map((tag) => (
            <span
              key={tag}
              className="rounded border border-border/50 px-1 text-[9px] text-muted-foreground"
            >
              {tag}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// ── Sidebar detail panel ──

function MemoryDetails({
  node,
  noteCount,
}: {
  node: MemoryGraphNode;
  noteCount?: number;
}) {
  const rows: [string, string | null][] =
    node.kind === "folder"
      ? [
          ["Path", node.path],
          ["Notes", String(noteCount ?? 0)],
        ]
      : [
          ["Path", node.path],
          ["Tag", node.primary_tag || "untagged"],
          ["Timestamp", node.timestamp],
          ["Agent", node.ava_agent],
          ["Machine", node.ava_machine],
        ];

  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold leading-tight">
          {node.title}
        </h3>
        {node.description ? (
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            {node.description}
          </p>
        ) : null}
      </div>
      <dl className="space-y-1.5 text-xs">
        {rows.map(([label, value]) => (
          <div
            key={label}
            className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-2"
          >
            <dt className="text-muted-foreground">{label}</dt>
            <dd className={cn("break-words font-mono", MIN_W_0)}>
              {value ?? "-"}
            </dd>
          </div>
        ))}
      </dl>
      {node.tags.length > 0 ? (
        <div className={cn("flex-wrap gap-1.5", FLEX)}>
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
  );
}
