"use client";

// A lane opened in place: the agent's own run timeline (the single-agent rows and details, reused
// as they are) on the cluster view's time axis. The checkpoint-backed read happens here, for this
// agent only, and only while its lane is open.

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useLayoutEffect, useRef, useState } from "react";

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

// The run timeline's own frame (border and padding) is about this wide on each side; the real offset is measured.
const FRAME_GUESS_PX = 13;

/**
 * Margins that put the rows' tracks exactly over the lanes' tracks: starting from `margins` (the
 * ones in effect), moves each edge by the gap between the first embedded track and the lane track.
 * Pure so the arithmetic is tested; `null` when already aligned within half a pixel.
 */
export function alignMargins(
  margins: { left: number; right: number },
  lane: { left: number; right: number },
  embedded: { left: number; right: number },
): { left: number; right: number } | null {
  const dl = lane.left - embedded.left;
  const dr = embedded.right - lane.right;
  if (Math.abs(dl) < 0.5 && Math.abs(dr) < 0.5) return null;
  return { left: margins.left + dl, right: margins.right + dr };
}

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
  const frameRef = useRef<HTMLDivElement>(null);
  const [margins, setMargins] = useState({ left: -FRAME_GUESS_PX, right: -FRAME_GUESS_PX });
  // The frame's chrome differs between renderers of the rows, so measure rather than assume.
  useLayoutEffect(() => {
    const frame = frameRef.current;
    const laneTrack = frame?.closest('[data-testid="cluster-chart"]')?.querySelector('[data-testid="cluster-lane"] [data-track]');
    const embeddedTrack = frame?.querySelector("[data-track]");
    if (!laneTrack || !embeddedTrack) return;
    const next = alignMargins(margins, laneTrack.getBoundingClientRect(), embeddedTrack.getBoundingClientRect());
    if (next !== null) setMargins(next);
  }, [margins, data !== undefined]); // eslint-disable-line react-hooks/exhaustive-deps -- re-measures when the rows appear and after each correction

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
      {/* Pull the rows' own frame out so that its tracks line up with the lanes' tracks. */}
      <div ref={frameRef} style={{ marginLeft: margins.left, marginRight: margins.right }}>
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
