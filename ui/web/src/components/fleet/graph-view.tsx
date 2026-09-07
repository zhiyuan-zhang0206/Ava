// Graph View — the force-directed alternative to the fleet task tree.
//
// Where the tree shows one relationship (spawn/fork lineage as nesting), this
// view renders the whole weighted relationship graph: spawn/fork lineage as
// structural springs, plus aggregated agent-to-agent message traffic as weaker
// springs whose pull (and on-screen opacity) scales with the edge weight. Node
// size encodes cumulative token consumption (log scale). When intermediate
// parent agents terminate, live descendants re-parent to their nearest live
// ancestor so lineage springs remain unbroken (matching agent-tree semantics).
//
// Rendering / interaction / parameters live in the shared ForceGraph (see
// force-graph.tsx) — this module is a thin wrapper: it fetches the fleet graph,
// adapts it to the shared node/edge model, and adds the time-window selector +
// empty states. The Task Graph renders the same canvas with square nodes.

"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";

import { WindowSelect } from "@/components/window-select";
import { STATS_WINDOW_LABELS, STATS_WINDOWS, type StatsWindowHours } from "@/lib/sidebar";
import type { AgentRow, PublicAgentStatus } from "@/lib/types";
import { useFleetGraph } from "@/lib/use-fleet-graph";
import {
  AGENTS_QUERY_KEY,
  TERMINATED_AGENTS_QUERY_KEY,
} from "@/lib/use-agents";

import {
  FORCE_DEFAULTS,
  useForceParams,
} from "./force-controls";
import {
  ForceGraph,
  type ForceGraphEdge,
  type ForceGraphNode,
} from "./force-graph";
import { FLEX, OVERFLOW_HIDDEN } from "@/lib/layout";
import { cn } from "@/lib/utils";

// Status -> text-color class; the circle paints with fill="currentColor" so the
// node palette stays identical to the sidebar's STATUS_DOT (same tokens, just
// expressed as text-* so it resolves for SVG fill, incl. the theme `destructive`).
// Raw lifecycle transitions are projected at graph ingest, so the canvas only
// accepts the same three public states as the sidebar.
const STATUS_TEXT: Record<PublicAgentStatus, string> = {
  running: "text-sky-500",
  idling: "text-emerald-500",
  terminated: "text-destructive",
};
const STATUS_PULSE: Record<PublicAgentStatus, boolean> = {
  running: false,
  idling: false,
  terminated: false,
};
// Per-day decay constant for the edge weight (see the backend formula). Held as a
// constant for now; an advanced settings panel to tune it is deferred.
const DECAY_LAMBDA = 0.5;

// DB-backed user settings key for this view's force knobs — the Task Graph
// keeps its own key so the two graphs' tunings stay independent.
const FORCE_PARAMS_KEY = "display.graph_force_params";

function parentIdOf(spawner: string): number | null {
  if (!spawner.startsWith("agent:")) return null;
  const n = Number(spawner.slice("agent:".length));
  return Number.isFinite(n) ? n : null;
}

interface LineageAgentInfo {
  readonly spawner: string;
  readonly fork_source_agent_id?: number | null;
}

/**
 * Walk up the lineage ancestor chain to find the nearest ancestor that is still
 * live in the graph view. Mirrors the recursive re-parenting in agent-tree.ts.
 */
function findNearestLiveAncestor(
  agentId: number,
  byId: Map<number, LineageAgentInfo>,
  liveIds: Set<number>,
): { ancestorId: number | null; isFork: boolean } {
  let currId = agentId;
  let directIsFork = false;
  const visited = new Set<number>();

  for (;;) {
    if (visited.has(currId)) break;
    visited.add(currId);

    const node = byId.get(currId);
    if (!node) break;

    const parentId = node.fork_source_agent_id ?? parentIdOf(node.spawner);
    const isFork = node.fork_source_agent_id != null;

    if (currId === agentId) {
      directIsFork = isFork;
    }

    if (parentId == null) break;

    if (liveIds.has(parentId)) {
      return { ancestorId: parentId, isFork: directIsFork };
    }

    currId = parentId;
  }

  return { ancestorId: null, isFork: directIsFork };
}

export function GraphView({
  selectedAgentId,
  onSelectAgent,
}: {
  selectedAgentId: number | null;
  onSelectAgent: (id: number | null) => void;
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const t = useTranslations("fleet.graph");



  // Time window for node score + edge events (default 24h). Local to the graph
  // view (not synced to user settings yet — rapid window hopping is common).
  const [windowHours, setWindowHours] = useState<StatsWindowHours>(24);

  // User-tunable force-layout knobs (DB-backed: display.graph_force_params).
  const { params: forceParams, setParams: setForceParams, reset: resetForceParams } =
    useForceParams(FORCE_PARAMS_KEY, FORCE_DEFAULTS);

  // Liveness filter FIRST (user ruling 2026-08-09 #1104): terminated agents
  // never appear in the graph — mirroring the sidebar's agent tree
  // (agent-sidebar/body.tsx filters `status !== "terminated"`), so both
  // surfaces stay consistent. The backend endpoint filters at the SQL layer
  // too (payload), but the ruling's filter ORDER is liveness before anything
  // else — the component re-filters so a backend leak can never paint a
  // terminated node or its edges.
  const { graph, loading, error } = useFleetGraph({
    hours: windowHours,
    decayLambda: DECAY_LAMBDA,
  });

  const statusLabels: Record<PublicAgentStatus, string> = useMemo(
    () => ({
      running: t("running"),
      idling: t("idling"),
      terminated: t("terminated"),
    }),
    [t],
  );

  // Liveness filter — see the note above; mirrors agent-sidebar/body.tsx.
  const liveNodes = useMemo(
    () => graph.nodes.filter((n) => n.status !== "terminated"),
    [graph.nodes],
  );
  const liveIds = useMemo(
    () => new Set(liveNodes.map((n) => n.agent_id)),
    [liveNodes],
  );

  // Lineage lookup map covering both live and terminated nodes to trace ancestors.
  const lineageById = useMemo(() => {
    const liveRoster = queryClient.getQueryData<AgentRow[]>(AGENTS_QUERY_KEY) ?? [];
    const terminatedRoster =
      queryClient.getQueryData<AgentRow[]>(TERMINATED_AGENTS_QUERY_KEY) ?? [];

    const map = new Map<number, LineageAgentInfo>();
    for (const a of liveRoster) {
      map.set(a.agent_id, a);
    }
    for (const a of terminatedRoster) {
      map.set(a.agent_id, a);
    }
    for (const n of graph.nodes) {
      if (!map.has(n.agent_id)) {
        map.set(n.agent_id, n);
      }
    }
    return map;
  }, [queryClient, graph.nodes]);

  // Adapt the fleet graph to the shared node/edge model.
  // User ruling 2026-09-07 21:02: Every live agent remains in the graph (children
  // are never hidden). When intermediate parents terminate, their live descendants
  // re-parent to the nearest live ancestor in the edges collection below.
  const nodes = useMemo<ForceGraphNode[]>(
    () =>
      liveNodes.map((n) => ({
        id: n.agent_id,
        label: n.label,
        status: n.status,
        score: n.node_score,
        pulse: STATUS_PULSE[n.status],
      })),
    [liveNodes],
  );

  // The backend returns one edge per event kind (spawn / fork / resurrect /
  // message), and every non-message kind collapses to "lineage" here — so a
  // pair that fired several kinds would otherwise produce DUPLICATE React keys
  // (`${from}-${to}-${kind}`) downstream. Duplicate keys make React's
  // reconciliation leave orphaned <line> nodes behind on every layout tick:
  // stale copies of the same edge accumulate at old coordinates, floating in
  // space and overlapping (the "extra dangling edges" bug). Merge the lineage
  // family into one edge per pair — strongest weight wins, fork styling wins
  // if any member was a fork.
  const edges = useMemo<ForceGraphEdge[]>(() => {
    const byPair = new Map<string, ForceGraphEdge>();

    // 1. Process telemetry/Loki edges between currently live nodes.
    for (const e of graph.edges) {
      const from = e.from_agent;
      const to = e.to_agent;
      if (!liveIds.has(from) || !liveIds.has(to)) continue;
      const key = e.event_type === "message" ? `m:${from}:${to}` : `l:${from}:${to}`;
      const existing = byPair.get(key);
      if (!existing) {
        byPair.set(key, {
          from,
          to,
          kind: e.event_type === "message" ? "message" : "lineage",
          dashed: e.event_type === "fork",
          weight: e.weight,
        });
      } else {
        byPair.set(key, {
          from,
          to,
          kind: existing.kind,
          dashed: existing.dashed === true || e.event_type === "fork",
          weight: Math.max(existing.weight, e.weight),
        });
      }
    }

    // 2. Nearest live ancestor re-parenting: ensure each live agent connects to
    // its nearest live ancestor so that intermediate terminated nodes do not break
    // lineage ties (matching tree semantics: A -> B(term) -> C => A -> C).
    for (const node of liveNodes) {
      const { ancestorId, isFork } = findNearestLiveAncestor(
        node.agent_id,
        lineageById,
        liveIds,
      );
      if (ancestorId != null && liveIds.has(ancestorId)) {
        const key = `l:${ancestorId}:${node.agent_id}`;
        const existing = byPair.get(key);
        if (!existing) {
          byPair.set(key, {
            from: ancestorId,
            to: node.agent_id,
            kind: "lineage",
            dashed: isFork,
            weight: 2.0,
          });
        } else {
          byPair.set(key, {
            from: ancestorId,
            to: node.agent_id,
            kind: existing.kind,
            dashed: existing.dashed === true || isFork,
            weight: Math.max(existing.weight, 2.0),
          });
        }
      }
    }

    return [...byPair.values()];
  }, [graph.edges, liveNodes, liveIds, lineageById]);

  // When the selected agent disappears from the graph (e.g. it was
  // terminated) — clear the stale selection so the canvas and selection stay in sync.
  useEffect(() => {
    if (
      selectedAgentId != null &&
      !nodes.some((n) => n.id === selectedAgentId)
    ) {
      onSelectAgent(null);
    }
  }, [nodes, selectedAgentId, onSelectAgent]);

  // Instant hover card — the shared canvas shows it the moment the cursor
  // enters a node (replacing the delayed native <title>): identity, status
  // and the activity score that drives node size.
  const agentHoverCard = useCallback(
    (node: ForceGraphNode) => (
      <div className="w-52 rounded-lg border border-border bg-popover/95 p-3 shadow-xl backdrop-blur">
        <p className="line-clamp-2 break-words text-xs font-semibold leading-snug text-popover-foreground">
          {node.label ?? t("unlabeledAgent")}
        </p>
        <p className="mt-0.5 text-[10px] tabular-nums text-muted-foreground">
          {t("agent", { id: node.id })}
        </p>
        <div className="mt-2 space-y-1 text-[11px]">
          <p className={cn("items-center gap-1.5", FLEX)}>
            <span
              className={cn("size-2 rounded-full bg-current", STATUS_TEXT[node.status as PublicAgentStatus])}
            />
            {statusLabels[node.status as PublicAgentStatus]}
          </p>
          <p className="text-muted-foreground">
            {t("activityScore", { score: `${(node.score / 1_000_000).toFixed(2)}M` })}
          </p>
        </div>
      </div>
    ),
    [statusLabels, t],
  );

  // Stale age indicator — tick every 30s so "Xm ago" advances while the tab is open.
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNowMs(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, []);

  const snapshotAge = graph.snapshot_at ? nowMs - Date.parse(graph.snapshot_at) : null;
  const snapshotMinutes = snapshotAge != null ? Math.floor(snapshotAge / 60_000) : null;
  const snapshotAgeLabel =
    snapshotMinutes != null
      ? snapshotMinutes < 1
        ? t("snapshotNow")
        : snapshotMinutes < 60
          ? t("snapshotMinutes", { count: snapshotMinutes })
          : t("snapshotHours", { count: Math.floor(snapshotMinutes / 60) })
      : null;

  return (
    <div className={cn("relative h-full w-full", OVERFLOW_HIDDEN)}>
      <ForceGraph
        nodes={nodes}
        edges={edges}
        shape="circle"
        statusText={STATUS_TEXT}
        selectedId={selectedAgentId}
        onSelect={onSelectAgent}
        onOpen={(id) => router.push(`/?agent_id=${id}`)}
        params={forceParams}
        setParams={setForceParams}
        resetParams={resetForceParams}
        hoverCard={agentHoverCard}
        statsText={t("stats", { nodes: nodes.length, edges: edges.length })}
        legend={
          <div aria-label={t("legend")} className="space-y-1">
            <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
              {(
                [
                  "running",
                  "idling",
                ] as const
              ).map((status) => (
                <span key={status} className={cn("items-center gap-1.5", FLEX)}>
                  <span className={cn("size-2 rounded-full bg-current", STATUS_TEXT[status])} />
                  {statusLabels[status]}
                </span>
              ))}
            </div>
          </div>
        }
        ariaLabel={t("ariaLabel")}
        overlayLeft={
          <WindowSelect
            value={String(windowHours)}
            options={STATS_WINDOWS.map((h) => ({ value: String(h), label: STATS_WINDOW_LABELS[h] }))}
            onChange={(v) => setWindowHours(Number(v) as StatsWindowHours)}
            ariaLabel={t("window")}
            className="cursor-pointer rounded border border-border bg-background/80 px-1.5 py-0.5 text-[10px] text-muted-foreground backdrop-blur hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
        }
      />
      {graph.stale ? (
        <p
          role="status"
          className="pointer-events-none absolute right-3 top-3 inline-flex items-center gap-1 rounded border border-amber-500/30 bg-background/80 px-2 py-1 text-[10px] text-amber-600 backdrop-blur dark:text-amber-400"
        >
          <span aria-hidden className="size-1.5 rounded-full bg-amber-500" />
          {snapshotAge
            ? t("staleSnapshot", { age: snapshotAgeLabel ?? "" })
            : t("staleLastKnown")}
        </p>
      ) : graph.telemetry_stale ? (
        <p
          role="status"
          className="pointer-events-none absolute right-3 top-3 inline-flex items-center gap-1 rounded border border-border bg-background/80 px-2 py-1 text-[10px] text-muted-foreground backdrop-blur"
        >
          <span aria-hidden className="size-1.5 rounded-full bg-muted-foreground" />
          {t("telemetryDegraded")}
        </p>
      ) : null}
      {graph.truncated ? (
        <p
          role="status"
          className="pointer-events-none absolute right-3 top-10 inline-flex items-center gap-1 rounded border border-orange-500/30 bg-background/80 px-2 py-1 text-[10px] text-orange-600 backdrop-blur dark:text-orange-400"
        >
          <span aria-hidden className="size-1.5 rounded-full bg-orange-500" />
          {t("truncated")}
        </p>
      ) : null}
      {nodes.length === 0 ? (
        <p className={cn("absolute inset-0 items-center justify-center text-xs text-muted-foreground", FLEX)}>
          {loading ? t("loading") : error ? t("unavailable") : t("empty")}
        </p>
      ) : null}
    </div>
  );
}
