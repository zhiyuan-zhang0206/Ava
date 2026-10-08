"use client";

// A lane opened in place: the agent's own run timeline (the single-agent rows and details, reused
// as they are) on the cluster view's time axis. The checkpoint-backed read happens here, for this
// agent only, and only while its lane is open.

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { NodeDetail, UnitDetail } from "@/components/run-timeline/run-timeline-detail";
import { RunTimelineRows } from "@/components/run-timeline/run-timeline-rows";
import {
  nodeAncestors,
  nodeChildren,
  requestSelection,
  type Highlight,
  type Selection,
  type Viewport,
} from "@/components/run-timeline/timeline-model";
import { api } from "@/lib/transport/api";

import { windowIso } from "./cluster-model";

export function LaneExpansion({
  agentId,
  base,
  view,
  onView,
  loaded,
}: {
  agentId: number;
  /** The whole extent of the cluster view: the axis never leaves it. */
  base: Viewport;
  view: Viewport;
  onView: (view: Viewport) => void;
  /** The window the agent's run timeline is read for (the cluster view's loaded window). */
  loaded: Viewport;
}) {
  const t = useTranslations("clusterView");
  const [selection, setSelection] = useState<Selection | null>(null);
  const [highlight, setHighlight] = useState<Highlight | null>(null);
  const range = windowIso(loaded);
  const query = useQuery({
    queryKey: ["run-timeline", agentId, range.from, range.to],
    queryFn: () => api.getRunTimeline(agentId, range),
    placeholderData: keepPreviousData,
  });
  const data = query.data;

  if (data === undefined) {
    return query.isError ? (
      <div className="space-y-2 font-mono text-xs text-destructive" role="alert">
        <p>{t("expansionFailed", { agentId })}</p>
        <button type="button" className="rounded border border-border px-2 py-0.5" onClick={() => void query.refetch()}>
          {t("retry")}
        </button>
      </div>
    ) : (
      <div className="h-24 animate-pulse rounded bg-muted/40" data-testid="cluster-expansion-loading" aria-hidden="true" />
    );
  }

  const selectedNode =
    selection?.kind === "node" ? data.nodes.find((node) => node.id === selection.id) : undefined;
  const unitTarget = selection?.kind === "request" ? requestSelection({ idx: selection.idx }, data.units) : selection;
  const selectedUnit =
    unitTarget?.kind === "unit"
      ? data.units.find(
          (unit) => unit.i0 === unitTarget.i0 && unit.i1 === unitTarget.i1 && unit.kind === unitTarget.unitKind,
        )
      : undefined;

  return (
    <div className="space-y-2" data-testid="cluster-expansion">
      {/* The rows' own frame (border and padding) is 13px each side: pull it out so its track lines up with the lanes' tracks. */}
      <div className="-mx-[13px]">
        <RunTimelineRows
          data={data}
          base={base}
          view={view}
          onView={onView}
          selection={selection}
          onSelect={setSelection}
          highlight={highlight}
          onHighlight={setHighlight}
        />
      </div>
      {selectedNode ? (
        <div className="rounded border border-border p-3">
          <NodeDetail
            key={`n${selectedNode.id}`}
            agentId={agentId}
            node={selectedNode}
            ancestors={nodeAncestors(selectedNode, data.nodes)}
            childNodes={nodeChildren(selectedNode, data.nodes)}
            onSelectNode={(id) => setSelection({ kind: "node", id })}
          />
        </div>
      ) : selectedUnit ? (
        <div className="rounded border border-border p-3">
          <UnitDetail
            key={`u${selectedUnit.kind}${selectedUnit.i0}-${selectedUnit.i1}`}
            agentId={agentId}
            unit={selectedUnit}
            parent={data.nodes.find((node) => node.id === selectedUnit.parent) ?? null}
            onSelectNode={(id) => setSelection({ kind: "node", id })}
          />
        </div>
      ) : null}
    </div>
  );
}
