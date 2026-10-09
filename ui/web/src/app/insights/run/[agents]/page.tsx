"use client";

import { useQueries, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";

import { ContextBreakdownCard, type CategoryHighlight } from "@/components/inspector/context-breakdown";
import { AgentViewToolbar } from "@/components/run-timeline/agent-view-toolbar";
import { AgentPending } from "@/components/run-timeline/agent-view-group";
import type { AgentSelection } from "@/components/run-timeline/agent-view-nav";
import { NodeDetail, UnitDetail } from "@/components/run-timeline/run-timeline-detail";
import { RunTimelineRows, type AgentEntry } from "@/components/run-timeline/run-timeline-rows";
import { RunTimelineWorkspace } from "@/components/run-timeline/run-timeline-workspace";
import { PageHeader } from "@/components/shell/page-header";
import {
  categoryClass,
  classCategory,
  clampViewport,
  contextPoint,
  levelsTopFirst,
  nodeAncestors,
  nodeChildren,
  requestSelection,
  viewportOf,
  type Highlight,
  type Selection,
  type Viewport,
} from "@/components/run-timeline/timeline-model";
import type { ContextBars, RowOptions } from "@/components/run-timeline/timeline-nav";
import type { RunTimelineResponse } from "@/lib/contracts/types";
import { api } from "@/lib/transport/api";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout/layout";
import { formatAbsolute } from "@/lib/format/time";
import { cn } from "@/lib/format/utils";

/** The agent ids of the URL segment (`405` or `405,6657`); null when any is not a non-negative integer. */
function parseAgents(segment: string): number[] | null {
  const ids: number[] = [];
  for (const part of decodeURIComponent(segment).split(",")) {
    const id = Number(part);
    if (part.trim() === "" || !Number.isInteger(id) || id < 0) return null;
    if (!ids.includes(id)) ids.push(id);
  }
  return ids;
}

const NO_AGENTS: number[] = [];

/** What the page keeps of each agent's query; shared by structure, so an unchanged read keeps its identity. */
const pickRead = (results: { data: RunTimelineResponse | undefined; isError: boolean }[]) =>
  results.map(({ data, isError }) => ({ data, isError }));

const pathFor = (ids: readonly number[]) => `/insights/run/${ids.join(",")}`;

/** The agent view: any number of agents on one timeline, each with every level of its understanding
 *  tree and, under them, its message units. One agent is the view of that agent alone. The backend
 *  answers once per agent with its whole lifetime; zooming and panning move a shared viewport over
 *  the loaded data (no further reads). */
export default function AgentViewPage({ params }: { params: Promise<{ agents: string }> }) {
  const t = useTranslations("runTimeline");
  // The agents in view, in the order they were added; the URL names them so a reload keeps them.
  const [agentIds, setAgentIds] = useState<number[] | null>(null);
  const [paramsResolved, setParamsResolved] = useState(false);
  const [selection, setSelection] = useState<AgentSelection | null>(null);
  // The legend's (or a context breakdown row's) highlight of one kind of block; it survives zoom and pan.
  const [highlight, setHighlight] = useState<Highlight | null>(null);
  // null = the whole loaded extent.
  const [viewport, setViewport] = useState<Viewport | null>(null);
  const [levels, setLevels] = useState<number | null>(null);
  const [context, setContext] = useState<ContextBars>("added");
  const options: RowOptions = useMemo(() => ({ levels, context }), [levels, context]);

  useEffect(() => {
    let cancelled = false;
    params
      .then(({ agents }) => {
        if (!cancelled) {
          setAgentIds(parseAgents(agents));
          setParamsResolved(true);
        }
      })
      .catch(() => {
        if (!cancelled) setParamsResolved(true);
      });
    return () => {
      cancelled = true;
    };
  }, [params]);

  const ids = agentIds ?? NO_AGENTS;
  const setAgents = (next: number[]) => {
    setAgentIds(next);
    // The extent changes with the agents: show all of it again.
    setViewport(null);
    window.history.replaceState(null, "", pathFor(next));
  };
  const addAgent = (id: number) => {
    if (!ids.includes(id)) setAgents([...ids, id]);
  };
  const removeAgent = (id: number) => {
    if (ids.length <= 1) return;
    if (selection?.agent === id) setSelection(null);
    setAgents(ids.filter((other) => other !== id));
  };

  const queryClient = useQueryClient();
  const reads = useQueries({
    queries: ids.map((id) => ({
      queryKey: ["run-timeline", id],
      queryFn: () => api.getRunTimeline(id, {}),
    })),
    combine: pickRead,
  });
  const entries: AgentEntry[] = useMemo(
    () =>
      ids.map((id, i): AgentEntry => {
        const read = reads.at(i);
        if (read?.data !== undefined) return { id, status: "loaded", data: read.data };
        return { id, status: read?.isError ? "failed" : "loading" };
      }),
    [ids, reads],
  );
  const retry = (id: number) => void queryClient.refetchQueries({ queryKey: ["run-timeline", id] });
  const loaded = useMemo(
    () => entries.flatMap((entry) => (entry.status === "loaded" ? [{ id: entry.id, data: entry.data }] : [])),
    [entries],
  );

  if (paramsResolved && agentIds === null) {
    return (
      <main id="main-content">
        <p className="p-6 font-mono text-sm text-destructive">{t("invalidAgent")}</p>
      </main>
    );
  }

  // The extent every agent's lifetime spans together.
  const windows = loaded.map(({ data }) => viewportOf(data.window));
  const base: Viewport | null =
    windows.length === 0
      ? null
      : { from: Math.min(...windows.map((w) => w.from)), to: Math.max(...windows.map((w) => w.to)) };
  const view = base && viewport ? clampViewport(viewport, base) : base;

  const dataOf = (id: number) => loaded.find((agent) => agent.id === id)?.data;
  // The selection's own agent; else the first loaded one, which the context card follows.
  const focus = selection !== null && dataOf(selection.agent) !== undefined ? selection.agent : loaded.at(0)?.id;
  const focusData = focus === undefined ? undefined : dataOf(focus);
  const mine = selection !== null && selection.agent === focus ? selection.selection : null;
  const selectedNode =
    mine?.kind === "node" ? focusData?.nodes.find((node) => node.id === mine.id) : undefined;
  // A selected request shows the details of the block of the AIMessage that made it.
  const unitTarget: Selection | null =
    mine?.kind === "request" ? requestSelection({ idx: mine.idx }, focusData?.units ?? []) : mine;
  const selectedUnit =
    unitTarget?.kind === "unit"
      ? focusData?.units.find(
          (unit) =>
            unit.i0 === unitTarget.i0 && unit.i1 === unitTarget.i1 && unit.kind === unitTarget.unitKind,
        )
      : undefined;

  // The context card follows the selected block, else the last LLM request in view.
  const contextAt = focusData && view ? contextPoint(mine, focusData.nodes, focusData.requests, view) : undefined;
  const categoryHighlight: CategoryHighlight = {
    active: highlight === null ? null : classCategory(highlight.cls),
    has: (category) => categoryClass(category) !== null,
    onToggle: (category) => {
      const cls = categoryClass(category);
      if (cls === null) return;
      setHighlight(highlight?.cls === cls ? null : { cls, source: null });
    },
  };
  const maxLevels = Math.max(0, ...loaded.map(({ data }) => levelsTopFirst(data.nodes).length));
  const select = (agent: number) => (id: string) => setSelection({ agent, selection: { kind: "node", id } });

  const main = (
    <>
      <AgentViewToolbar
        agentIds={ids}
        onAdd={addAgent}
        levels={levels}
        maxLevels={maxLevels}
        onLevels={setLevels}
        context={context}
        onContext={setContext}
      />
      {base && view ? (
        <>
          <p className="text-xs text-muted-foreground" data-testid="run-timeline-window">
            {t("windowSummary", {
              from: formatAbsolute(new Date(view.from).toISOString()),
              to: formatAbsolute(new Date(view.to).toISOString()),
              nodes: loaded.reduce((sum, { data }) => sum + data.nodes.length, 0),
              units: loaded.reduce((sum, { data }) => sum + data.units.length, 0),
            })}
          </p>
          <RunTimelineRows
            entries={entries}
            base={base}
            view={view}
            onView={setViewport}
            selection={selection}
            onSelect={setSelection}
            highlight={highlight}
            onHighlight={setHighlight}
            options={options}
            onRemove={ids.length > 1 ? removeAgent : null}
            onRetry={retry}
          />
        </>
      ) : (
        <div className="space-y-3">
          {entries.map((entry) => (
            <AgentPending
              key={entry.id}
              agentId={entry.id}
              failed={entry.status === "failed"}
              onRetry={retry}
              onRemove={ids.length > 1 ? removeAgent : null}
            />
          ))}
        </div>
      )}
      {/* Agent-scoped context details follow the timeline. */}
      {focus !== undefined && contextAt !== undefined ? (
        <ContextBreakdownCard agentId={focus} at={contextAt} categoryHighlight={categoryHighlight} />
      ) : null}
    </>
  );

  const side = selectedNode ? (
    <NodeDetail
      key={`n${focus}-${selectedNode.id}`}
      agentId={focus ?? 0}
      node={selectedNode}
      ancestors={focusData ? nodeAncestors(selectedNode, focusData.nodes) : []}
      childNodes={focusData ? nodeChildren(selectedNode, focusData.nodes) : []}
      onSelectNode={focus === undefined ? () => undefined : select(focus)}
    />
  ) : selectedUnit ? (
    <UnitDetail
      key={`u${focus}-${selectedUnit.kind}${selectedUnit.i0}-${selectedUnit.i1}`}
      agentId={focus ?? 0}
      unit={selectedUnit}
      parent={focusData?.nodes.find((node) => node.id === selectedUnit.parent) ?? null}
      onSelectNode={focus === undefined ? () => undefined : select(focus)}
    />
  ) : (
    <p className="text-sm text-muted-foreground">{t("detailEmpty")}</p>
  );

  return (
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <PageHeader title={t("title")} backHref="/insights" backLabel={t("backToInsights")} />
      <RunTimelineWorkspace main={main} side={side} />
    </main>
  );
}
