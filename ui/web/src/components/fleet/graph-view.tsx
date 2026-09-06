// Graph View — the force-directed alternative to the fleet task tree.
//
// Where the tree shows one relationship (spawn/fork lineage as nesting), this
// view renders the whole weighted relationship graph: spawn/fork lineage as
// structural springs, plus aggregated agent-to-agent message traffic as weaker
// springs whose pull (and on-screen opacity) scales with the edge weight. Node
// size encodes cumulative token consumption (log scale). Degree-0 (orphan)
// nodes float naturally with the same physics — no special arrangement
// (user ruling 2026-08-06: the isolate grid was removed).
//
// Rendering / interaction / parameters live in the shared ForceGraph (see
// force-graph.tsx) — this module is a thin wrapper: it fetches the fleet graph,
// adapts it to the shared node/edge model, and adds the time-window selector +
// empty states. The Task Graph renders the same canvas with square nodes.

"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { WindowSelect } from "@/components/window-select";
import { STATS_WINDOW_LABELS, STATS_WINDOWS, type StatsWindowHours } from "@/lib/sidebar";
import type { PublicAgentStatus } from "@/lib/types";
import { useFleetGraph } from "@/lib/use-fleet-graph";

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
const OFFLINE_STATUS = "offline";
type GraphDisplayStatus = PublicAgentStatus | typeof OFFLINE_STATUS;
const STATUS_TEXT: Record<GraphDisplayStatus, string> = {
  running: "text-sky-500",
  idling: "text-emerald-500",
  terminated: "text-destructive",
  offline: "text-muted-foreground",
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

type SnapshotAge =
  | { unit: "now" }
  | { unit: "minutes"; count: number }
  | { unit: "hours"; count: number }
  | { unit: "days"; count: number };

function formatSnapshotAge(snapshotAt: string): SnapshotAge | null {
  const snapshotMs = Date.parse(snapshotAt);
  if (Number.isNaN(snapshotMs)) return null;

  const minutes = Math.max(0, Math.floor((Date.now() - snapshotMs) / 60_000));
  if (minutes < 1) return { unit: "now" };
  if (minutes < 60) return { unit: "minutes", count: minutes };

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return { unit: "hours", count: hours };
  return { unit: "days", count: Math.floor(hours / 24) };
}

export function GraphView({
  selectedAgentId,
  onSelectAgent,
}: {
  selectedAgentId: number | null;
  onSelectAgent: (id: number | null) => void;
}) {
  const t = useTranslations("fleet.graph");
  const router = useRouter();
  // Time window for node score + edge events (default 24h). Local to the graph
  // view — independent of the sidebar's persisted stats window.
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
  // terminated node or its edges. A live node whose lineage partner has since
  // terminated simply shows without that edge (no ghost nodes).
  const { graph, loading, error } = useFleetGraph({
    hours: windowHours,
    decayLambda: DECAY_LAMBDA,
  });
  const snapshotAge = graph.snapshot_at ? formatSnapshotAge(graph.snapshot_at) : null;
  const snapshotAgeLabel =
    snapshotAge?.unit === "now"
      ? t("snapshotNow")
      : snapshotAge?.unit === "minutes"
        ? t("snapshotMinutes", { count: snapshotAge.count })
        : snapshotAge?.unit === "hours"
          ? t("snapshotHours", { count: snapshotAge.count })
          : snapshotAge?.unit === "days"
            ? t("snapshotDays", { count: snapshotAge.count })
            : null;
  const statusLabels = useMemo<Record<GraphDisplayStatus, string>>(
    () => ({
      running: t("running"),
      idling: t("idling"),
      terminated: t("terminated"),
      offline: t("offline"),
    }),
    [t],
  );

  // A selected agent that was in the graph and then disappeared (transitioned to
  // terminated) — clear the stale selection so the canvas and selection stay in sync.
  // Agents selected from outside the graph (e.g. Task Graph) whose id is not in the
  // node set are not cleared — they were never in the graph to begin with.
  const prevGraphNodeIds = useRef<Set<number>>(new Set());
  useEffect(() => {
    // Track which agent ids have ever appeared in the graph nodes.
    const currentIds = new Set(graph.nodes.map((n) => n.agent_id));
    // Merge current ids into the accumulated set so we remember agents that
    // were once in the graph but have since dropped out.
    for (const id of currentIds) prevGraphNodeIds.current.add(id);
    // Clear selection only when the selected agent was previously in the graph
    // but is no longer there (it transitioned to terminated while selected).
    if (
      selectedAgentId != null &&
      !currentIds.has(selectedAgentId) &&
      prevGraphNodeIds.current.has(selectedAgentId)
    ) {
      onSelectAgent(null);
    }
  }, [graph.nodes, selectedAgentId, onSelectAgent]);

  // Liveness filter — see the note above; mirrors agent-sidebar/body.tsx.
  const liveNodes = useMemo(
    () => graph.nodes.filter((n) => n.status !== "terminated"),
    [graph.nodes],
  );
  const liveIds = useMemo(
    () => new Set(liveNodes.map((n) => n.agent_id)),
    [liveNodes],
  );

  // Adapt the fleet graph to the shared node/edge model.
  const nodes = useMemo<ForceGraphNode[]>(
    () =>
      liveNodes.map((n) => ({
        id: n.agent_id,
        label: n.label,
        status: n.liveness_state === "offline" ? OFFLINE_STATUS : n.status,
        score: n.node_score,
        pulse: STATUS_PULSE[n.status],
      })),
    [liveNodes],
  );

  // Instant hover card — the shared canvas shows it the moment the cursor
  // enters a node (replacing the delayed native <title>): identity, status
  // and the activity score that drives node size.
  const agentHoverCard = useCallback(
    (node: ForceGraphNode) => (
      <div className="w-52 rounded-lg border border-border bg-popover/95 p-3 shadow-xl backdrop-blur">
        <p className="line-clamp-2 break-words text-xs font-semibold leading-snug text-popover-foreground">
          {node.label ?? t("unlabeledAgent")}
        </p>
        <p className="mt-0.5 font-mono text-2xs tabular-nums text-muted-foreground">
          {t("agent", { id: node.id })}
        </p>
        <div className="mt-2 space-y-1 text-xs">
          <p className={cn("items-center gap-1.5", FLEX)}>
            <span
              className={cn("size-2 rounded-full bg-current", STATUS_TEXT[node.status as GraphDisplayStatus])}
            />
            {statusLabels[node.status as GraphDisplayStatus]}
          </p>
          <p className="text-muted-foreground">
            {t("activityScore", { score: Math.round(node.score).toLocaleString() })}
          </p>
        </div>
      </div>
    ),
    [statusLabels, t],
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
    for (const e of graph.edges) {
      const from = e.from_agent;
      const to = e.to_agent;
      // Same liveness rule as the node filter: a line whose endpoint is not
      // live cannot be drawn — drop it here so a backend leak can't paint a
      // terminated node's edge either.
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
    return [...byPair.values()];
  }, [graph.edges, liveIds]);

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
              {([
                "running",
                "idling",
                "terminated",
                "offline",
              ] as const).map((status) => (
                <span key={status} className={cn("items-center gap-1.5", FLEX)}>
                  <span className={cn("size-2 rounded-full bg-current", STATUS_TEXT[status])} />
                  {statusLabels[status]}
                </span>
              ))}
            </div>
            <p>{t("sizeActivity", { window: "24h" })}</p>
          </div>
        }
        ariaLabel={t("ariaLabel")}
        overlayLeft={
          <WindowSelect
            value={String(windowHours)}
            options={STATS_WINDOWS.map((h) => ({ value: String(h), label: STATS_WINDOW_LABELS[h] }))}
            onChange={(v) => setWindowHours(Number(v) as StatsWindowHours)}
            ariaLabel={t("window")}
            className="cursor-pointer rounded border border-border bg-background/80 px-1.5 py-0.5 text-2xs text-muted-foreground backdrop-blur hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
        }
      />
      {graph.stale ? (
        <p
          role="status"
          className="pointer-events-none absolute right-3 top-3 inline-flex items-center gap-1 rounded border border-amber-500/30 bg-background/80 px-2 py-1 text-2xs text-amber-600 backdrop-blur dark:text-amber-400"
        >
          <span aria-hidden className="size-1.5 rounded-full bg-amber-500" />
          {snapshotAge
            ? t("staleSnapshot", { age: snapshotAgeLabel ?? "" })
            : t("staleLastKnown")}
        </p>
      ) : graph.telemetry_stale ? (
        <p
          role="status"
          className="pointer-events-none absolute right-3 top-3 inline-flex items-center gap-1 rounded border border-border bg-background/80 px-2 py-1 text-2xs text-muted-foreground backdrop-blur"
        >
          <span aria-hidden className="size-1.5 rounded-full bg-muted-foreground" />
          {t("telemetryDegraded")}
        </p>
      ) : null}
      {graph.truncated ? (
        <p
          role="status"
          className="pointer-events-none absolute right-3 top-10 inline-flex items-center gap-1 rounded border border-orange-500/30 bg-background/80 px-2 py-1 text-2xs text-orange-600 backdrop-blur dark:text-orange-400"
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
