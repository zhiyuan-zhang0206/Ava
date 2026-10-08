"use client";

// The agent lanes under the curves, on the same time axis: one row per agent in spawn order, its
// understanding-tree nodes of one level above a bar of LLM activity, lifecycle markers across it.
// A lane folds its spawn subtree and opens in place into the agent's run timeline. Message edges
// are drawn over all lanes, from the sender's lane at the send time to the receiver's lane at the
// read time; they say nothing about what either agent was doing.

import { useLayoutEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import type { AgentLane, ClusterLanes, ClusterMessages, LaneBar, LaneNode } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout/layout";

import { firstLine, projectBox, type Viewport } from "../run-timeline/timeline-model";
import { ClusterEdges } from "./cluster-edges";
import { LaneExpansion } from "./cluster-lane-expansion";
import {
  drawnEdges,
  formatUsd,
  parentMap,
  visibleRows,
  type LaneRow,
} from "./cluster-model";

const NODE_LABEL_CHARS = 60;
function boxStyle(box: { left: number; width: number }): React.CSSProperties {
  return { left: `${box.left}%`, width: `max(${box.width}%, 2px)` };
}

function LaneTrack({
  lane,
  view,
  color,
  onHint,
}: {
  lane: AgentLane;
  view: Viewport;
  color: string;
  onHint: (text: string | null) => void;
}) {
  const t = useTranslations("clusterView");
  const hint = (text: string) => ({
    onMouseEnter: () => onHint(text),
    onMouseLeave: () => onHint(null),
  });
  const barText = (bar: LaneBar) =>
    t("barHint", {
      calls: bar.calls,
      cost: formatUsd(bar.cost_usd),
      input: bar.input_tokens,
      output: bar.output_tokens,
      seconds: Math.max((Date.parse(bar.end) - Date.parse(bar.start)) / 1000, 0).toFixed(1),
    });
  const nodeText = (node: LaneNode) => t("nodeHint", { level: node.level, summary: firstLine(node.summary, 200) });
  return (
    <div data-track="" className={cn("relative h-9 rounded bg-muted/40", MIN_W_0, OVERFLOW_HIDDEN, "grow")}>
      {lane.nodes.map((node) => {
        const box = projectBox(Date.parse(node.start), Date.parse(node.end), view);
        if (box === null) return null;
        return (
          <div
            key={node.id}
            data-testid="cluster-lane-node"
            title={node.summary}
            className="absolute top-1 h-4 truncate rounded-sm border border-border bg-background/70 px-1 text-[10px] leading-[14px] text-muted-foreground"
            style={boxStyle(box)}
            {...hint(nodeText(node))}
          >
            {firstLine(node.summary, NODE_LABEL_CHARS)}
          </div>
        );
      })}
      {lane.bars.map((bar) => {
        const box = projectBox(Date.parse(bar.start), Date.parse(bar.end), view);
        if (box === null) return null;
        return (
          <div
            key={bar.start}
            data-testid="cluster-lane-bar"
            className="absolute bottom-1 h-2.5 rounded-[2px]"
            style={{ ...boxStyle(box), background: color }}
            {...hint(barText(bar))}
          />
        );
      })}
      {lane.events.map((event) => {
        const box = projectBox(Date.parse(event.ts), Date.parse(event.ts), view);
        if (box === null) return null;
        return (
          <span
            key={`${event.kind}-${event.ts}`}
            data-testid="cluster-lane-event"
            title={event.kind}
            className="absolute inset-y-0 w-px bg-foreground/60"
            style={{ left: `${box.left}%` }}
          />
        );
      })}
    </div>
  );
}

function LaneLabel({
  row,
  color,
  open,
  onFold,
  onOpen,
}: {
  row: LaneRow;
  color: string;
  open: boolean;
  onFold: () => void;
  onOpen: () => void;
}) {
  const t = useTranslations("clusterView");
  const { lane } = row;
  return (
    <div
      className={cn(FLEX, "w-20 shrink-0 items-center gap-0.5 self-center text-[11px]")}
      style={{ paddingLeft: Math.min(lane.depth, 4) * 6 }}
    >
      {row.hasChildren ? (
        <button
          type="button"
          aria-label={row.folded ? t("unfold", { agentId: lane.agent_id }) : t("fold", { agentId: lane.agent_id })}
          aria-expanded={!row.folded}
          title={row.folded ? t("foldedHint", { count: row.hidden }) : undefined}
          onClick={onFold}
          className="w-3 shrink-0 text-muted-foreground hover:text-foreground"
        >
          {row.folded ? "▸" : "▾"}
        </button>
      ) : (
        <span className="w-3 shrink-0" aria-hidden="true" />
      )}
      <span className="size-2 shrink-0 rounded-full" style={{ background: color }} aria-hidden="true" />
      <button
        type="button"
        aria-expanded={open}
        aria-label={t("openLane", { agentId: lane.agent_id })}
        title={t("laneTitle", {
          kind: t(`kind.${lane.kind}`),
          status: lane.status,
          calls: lane.calls,
          cost: formatUsd(lane.cost_usd),
        })}
        onClick={onOpen}
        className={cn(MIN_W_0, "truncate font-mono hover:underline", open ? "font-semibold text-foreground" : "text-muted-foreground")}
      >
        #{lane.agent_id}
      </button>
      {row.folded && row.hidden > 0 ? (
        <span className="shrink-0 text-[9px] text-muted-foreground">+{row.hidden}</span>
      ) : null}
    </div>
  );
}

export function ClusterLanesRows({
  lanes,
  messages,
  view,
  base,
  onView,
  loaded,
  trackPx,
  colors,
  folded,
  onFold,
  open,
  onOpen,
  onHint,
}: {
  lanes: ClusterLanes;
  messages: ClusterMessages | undefined;
  view: Viewport;
  base: Viewport;
  onView: (view: Viewport) => void;
  loaded: Viewport;
  trackPx: number;
  colors: ReadonlyMap<number, string>;
  folded: ReadonlySet<number>;
  onFold: (agentId: number) => void;
  open: number | null;
  onOpen: (agentId: number) => void;
  onHint: (text: string | null) => void;
}) {
  const rows = visibleRows(lanes.lanes, folded);
  const edges = drawnEdges(messages?.edges ?? [], rows, parentMap(lanes.lanes));
  const containerRef = useRef<HTMLDivElement>(null);
  const [centers, setCenters] = useState<Map<number, number>>(new Map());

  // Lane heights change when a lane opens (its run timeline loads asynchronously), so the
  // connectors' vertical positions are measured from the laid-out rows, not computed.
  const measure = useRef<() => void>(() => undefined);
  useLayoutEffect(() => {
    measure.current = () => {
      const container = containerRef.current;
      if (!container) return;
      const next = new Map<number, number>();
      container.querySelectorAll<HTMLElement>("[data-lane-head]").forEach((head) => {
        next.set(Number(head.dataset.laneHead), head.offsetTop + head.offsetHeight / 2);
      });
      setCenters((previous) => {
        const same = previous.size === next.size && [...next].every(([id, y]) => previous.get(id) === y);
        return same ? previous : next;
      });
    };
    measure.current();
  });
  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => measure.current());
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={containerRef} className="relative space-y-1.5" data-testid="cluster-lanes">
      {rows.map((row) => {
        const id = row.lane.agent_id;
        const color = colors.get(id) ?? "var(--muted-foreground)";
        return (
          <div key={id} data-lane={id} className="space-y-1.5">
            <div data-lane-head={id} className={cn(FLEX, "items-stretch gap-2")} data-testid="cluster-lane">
              <LaneLabel row={row} color={color} open={open === id} onFold={() => onFold(id)} onOpen={() => onOpen(id)} />
              <LaneTrack lane={row.lane} view={view} color={color} onHint={onHint} />
            </div>
            {open === id ? (
              <LaneExpansion agentId={id} base={base} view={view} onView={onView} loaded={loaded} />
            ) : null}
          </div>
        );
      })}
      <ClusterEdges
        edges={edges}
        centers={centers}
        colors={colors}
        view={view}
        trackPx={trackPx}
        open={open}
        onHint={onHint}
      />
    </div>
  );
}
