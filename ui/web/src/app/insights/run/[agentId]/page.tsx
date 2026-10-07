"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

import { ContextBreakdownCard } from "@/components/context-breakdown";
import { RunTimelineCrumbs } from "@/components/run-timeline/run-timeline-crumbs";
import { NodeDetail, UnitDetail } from "@/components/run-timeline/run-timeline-detail";
import { RunTimelineRows } from "@/components/run-timeline/run-timeline-rows";
import { RunTimelineChartSkeleton } from "@/components/run-timeline/run-timeline-skeleton";
import { RunTimelineWorkspace } from "@/components/run-timeline/run-timeline-workspace";
import {
  blockClass,
  clampViewport,
  firstLine,
  nodeWindow,
  unitWindow,
  viewportOf,
  type Crumb,
  type Selection,
  type TimelineWindow,
  type Viewport,
} from "@/components/run-timeline/timeline-model";
import { buttonVariants } from "@/components/ui/button";
import { api } from "@/lib/api";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout";
import { formatAbsolute } from "@/lib/time";
import type { RunTimelineUnit } from "@/lib/types";
import { cn } from "@/lib/utils";

const CRUMB_LABEL_CHARS = 40;

/** The agent's run timeline: every level of its understanding tree and, under them, its
 *  message units. The backend answers once with the agent's whole lifetime; zooming,
 *  panning and drilling move a viewport over that loaded data (no further reads). Drilling
 *  a node or unit zooms to its span and pushes a breadcrumb, the breadcrumbs step back. */
export default function RunTimelinePage({ params }: { params: Promise<{ agentId: string }> }) {
  const t = useTranslations("runTimeline");
  const [agentId, setAgentId] = useState<number | null>(null);
  const [paramsResolved, setParamsResolved] = useState(false);
  const [trail, setTrail] = useState<Crumb[]>([]);
  const [selection, setSelection] = useState<Selection | null>(null);
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

  // The drill path belongs to one agent: the route resolving to another invalidates it.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- identity-keyed reset, not a render loop
    setTrail((previous) => (previous.length === 0 ? previous : []));
    setSelection(null);
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

  const unitKindLabel = (unit: RunTimelineUnit) =>
    ({
      human: t("blockHuman"),
      agent: t("blockAgent"),
      text: t("blockText"),
      thinking: t("blockThinking"),
      call: t("blockCall"),
      output: t("blockOutput"),
      note: t("blockNote"),
    })[blockClass(unit)];

  const pushCrumb = (crumb: TimelineWindow & { label: string }) => {
    setTrail((previous) => [...previous, crumb]);
    setViewport(viewportOf(crumb));
  };

  // A double-click selects as well: the details follow the block the view zoomed to.
  const drill = (target: Selection) => {
    if (!data) return;
    if (target.kind === "node") {
      const node = data.nodes.find((candidate) => candidate.id === target.id);
      if (!node) return;
      pushCrumb({
        ...nodeWindow(node),
        label: `${t("levelRow", { level: node.level })} · ${firstLine(node.summary, CRUMB_LABEL_CHARS)}`,
      });
    } else {
      const unit = data.units.find(
        (candidate) =>
          candidate.i0 === target.i0 && candidate.i1 === target.i1 && candidate.kind === target.unitKind,
      );
      if (!unit) return;
      pushCrumb({
        ...unitWindow(unit),
        label: `${unitKindLabel(unit)} · ${firstLine(unit.preview, CRUMB_LABEL_CHARS) || `#${unit.i0}`}`,
      });
    }
    setSelection(target);
  };

  const stepBack = (index: number) => {
    const kept = index < 0 ? [] : trail.slice(0, index + 1);
    setTrail(kept);
    const crumb = kept.at(-1);
    setViewport(crumb ? viewportOf(crumb) : null);
  };

  const selectedNode =
    selection?.kind === "node" ? data?.nodes.find((node) => node.id === selection.id) : undefined;
  const selectedUnit =
    selection?.kind === "unit"
      ? data?.units.find(
          (unit) =>
            unit.i0 === selection.i0 && unit.i1 === selection.i1 && unit.kind === selection.unitKind,
        )
      : undefined;

  const main = (
    <>
      <RunTimelineCrumbs trail={trail} onSelect={stepBack} />
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
            onDrill={drill}
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
      {agentId !== null ? <ContextBreakdownCard agentId={agentId} /> : null}
    </>
  );

  const side = selectedNode ? (
    <NodeDetail
      key={`n${selectedNode.id}`}
      agentId={agentId ?? 0}
      node={selectedNode}
      onDrill={() => drill({ kind: "node", id: selectedNode.id })}
    />
  ) : selectedUnit ? (
    <UnitDetail
      key={`u${selectedUnit.kind}${selectedUnit.i0}-${selectedUnit.i1}`}
      agentId={agentId ?? 0}
      unit={selectedUnit}
      onDrill={() => drill({ kind: "unit", i0: selectedUnit.i0, i1: selectedUnit.i1, unitKind: selectedUnit.kind })}
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
