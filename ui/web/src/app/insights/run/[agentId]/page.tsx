"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

import { ContextBreakdownCard, type CategoryHighlight } from "@/components/inspector/context-breakdown";
import { NodeDetail, UnitDetail } from "@/components/run-timeline/run-timeline-detail";
import { RunTimelineRows } from "@/components/run-timeline/run-timeline-rows";
import { RunTimelineChartSkeleton } from "@/components/run-timeline/run-timeline-skeleton";
import { RunTimelineWorkspace } from "@/components/run-timeline/run-timeline-workspace";
import {
  categoryClass,
  classCategory,
  clampViewport,
  contextPoint,
  nodeAncestors,
  nodeChildren,
  requestSelection,
  viewportOf,
  type Highlight,
  type Selection,
  type Viewport,
} from "@/components/run-timeline/timeline-model";
import { buttonVariants } from "@/components/ui/button";
import { api } from "@/lib/transport/api";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { formatAbsolute } from "@/lib/format/time";
import { cn } from "@/lib/format/utils";

/** The agent's run timeline: every level of its understanding tree and, under them, its
 *  message units. The backend answers once with the agent's whole lifetime; zooming and
 *  panning move a viewport over that loaded data (no further reads). */
export default function RunTimelinePage({ params }: { params: Promise<{ agentId: string }> }) {
  const t = useTranslations("runTimeline");
  const [agentId, setAgentId] = useState<number | null>(null);
  const [paramsResolved, setParamsResolved] = useState(false);
  const [selection, setSelection] = useState<Selection | null>(null);
  // The legend's (or a context breakdown row's) highlight of one kind of block; it survives zoom and pan.
  const [highlight, setHighlight] = useState<Highlight | null>(null);
  // null = the whole loaded extent.
  const [viewport, setViewport] = useState<Viewport | null>(null);

  useEffect(() => {
    let cancelled = false;
    params
      .then(({ agentId: value }) => {
        const parsed = Number(value);
        if (!cancelled) {
          setAgentId(Number.isFinite(parsed) && parsed >= 0 ? parsed : null);
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

  // The selection, highlight and viewport belong to one agent: the route resolving to another invalidates them.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- identity-keyed reset, not a render loop
    setSelection(null);
    setHighlight(null);
    setViewport(null);
  }, [agentId]);

  const query = useQuery({
    queryKey: ["run-timeline", agentId],
    queryFn: () => api.getRunTimeline(agentId ?? 0, {}),
    enabled: agentId !== null,
  });
  const data = query.data;
  const base = data ? viewportOf(data.window) : null;
  const view = base && viewport ? clampViewport(viewport, base) : base;

  if (paramsResolved && agentId === null) {
    return (
      <main id="main-content">
        <p className="p-6 font-mono text-sm text-destructive">{t("invalidAgent")}</p>
      </main>
    );
  }

  const selectedNode =
    selection?.kind === "node" ? data?.nodes.find((node) => node.id === selection.id) : undefined;
  // A selected request shows the details of the block of the AIMessage that made it.
  const unitTarget: Selection | null =
    selection?.kind === "request"
      ? requestSelection({ idx: selection.idx }, data?.units ?? [])
      : selection;
  const selectedUnit =
    unitTarget?.kind === "unit"
      ? data?.units.find(
          (unit) =>
            unit.i0 === unitTarget.i0 && unit.i1 === unitTarget.i1 && unit.kind === unitTarget.unitKind,
        )
      : undefined;

  // The context card follows the selected block, else the last LLM request in view.
  const contextAt = data && view ? contextPoint(selection, data.nodes, data.requests, view) : undefined;
  const categoryHighlight: CategoryHighlight = {
    active: highlight === null ? null : classCategory(highlight.cls),
    has: (category) => categoryClass(category) !== null,
    onToggle: (category) => {
      const cls = categoryClass(category);
      if (cls === null) return;
      setHighlight(highlight?.cls === cls ? null : { cls, source: null });
    },
  };

  const main = (
    <>
      {data && base && view ? (
        <>
          <p className="text-xs text-muted-foreground" data-testid="run-timeline-window">
            {t("windowSummary", {
              from: formatAbsolute(new Date(view.from).toISOString()),
              to: formatAbsolute(new Date(view.to).toISOString()),
              nodes: data.nodes.length,
              units: data.units.length,
            })}
          </p>
          <RunTimelineRows
            data={data}
            base={base}
            view={view}
            onView={setViewport}
            selection={selection}
            onSelect={setSelection}
            highlight={highlight}
            onHighlight={setHighlight}
          />
        </>
      ) : query.isPending ? (
        <RunTimelineChartSkeleton />
      ) : (
        <div className="space-y-2 font-mono text-sm text-destructive" role="alert">
          <p>{t("loadFailed")}</p>
          <button type="button" className={buttonVariants({ size: "sm" })} onClick={() => void query.refetch()}>
            {t("retry")}
          </button>
        </div>
      )}
      {/* Agent-scoped context details follow the timeline. */}
      {agentId !== null && contextAt !== undefined ? (
        <ContextBreakdownCard agentId={agentId} at={contextAt} categoryHighlight={categoryHighlight} />
      ) : null}
    </>
  );

  const side = selectedNode ? (
    <NodeDetail
      key={`n${selectedNode.id}`}
      agentId={agentId ?? 0}
      node={selectedNode}
      ancestors={data ? nodeAncestors(selectedNode, data.nodes) : []}
      childNodes={data ? nodeChildren(selectedNode, data.nodes) : []}
      onSelectNode={(id) => setSelection({ kind: "node", id })}
    />
  ) : selectedUnit ? (
    <UnitDetail
      key={`u${selectedUnit.kind}${selectedUnit.i0}-${selectedUnit.i1}`}
      agentId={agentId ?? 0}
      unit={selectedUnit}
      parent={data?.nodes.find((node) => node.id === selectedUnit.parent) ?? null}
      onSelectNode={(id) => setSelection({ kind: "node", id })}
    />
  ) : (
    <p className="text-sm text-muted-foreground">{t("detailEmpty")}</p>
  );

  return (
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header className={cn("items-center gap-3 border-b border-border px-4 py-2", FLEX)}>
        <Link href="/insights" className={buttonVariants({ size: "sm", variant: "ghost" })}>
          {t("backToInsights")}
        </Link>
        <div className={cn(FLEX_1, MIN_W_0)}>
          <h1 className="truncate text-sm font-semibold">{t("title", { agentId: agentId ?? "—" })}</h1>
        </div>
      </header>
      <RunTimelineWorkspace main={main} side={side} />
    </main>
  );
}
