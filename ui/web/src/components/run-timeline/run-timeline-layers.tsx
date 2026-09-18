"use client";

// Narrative-layer rendering for the run timeline: the layer-block geometry
// drawn inside the chart SVG, the matching clickable overlay buttons, the
// pending-placeholder geometry/buttons (B, task #3981), and the raw-context
// summary band. Extracted from run-timeline-chart.tsx to keep that file under
// the source budget.

import type { FocusEvent, PointerEvent } from "react";

import type { RunTimelineResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

import {
  layerFocusLabel,
  layerNodeLabel,
  TIMELINE_POPOVER_ID,
  type RunTimelineChartLabels,
} from "./run-timeline-details";
import type { TimelineWindowOverride } from "./request-level";
import type { TimelineLayerRowLayout, TimelinePendingSpanLayout } from "./timeline-layout";

interface LayerTrackProps {
  rows: TimelineLayerRowLayout[];
  layers: NonNullable<RunTimelineResponse["layers"]>;
}

export function LayerTrackGeometry({
  rows,
  selectedIndex,
  pathIndexes,
  dimmed,
}: {
  rows: TimelineLayerRowLayout[];
  selectedIndex: number | null;
  /** P4-2 (#4023): chain nodes covering the selected message — outlined as
   *  the message's path through the summary stack (demo `.blk.path`). */
  pathIndexes?: ReadonlySet<number>;
  /** P4-2: a legend category is active; non-selected blocks mute to 0.12,
   *  the demo's category-highlight rule. */
  dimmed?: boolean;
}) {
  return (
    <>
      {rows.flatMap((layerRow) =>
        layerRow.blocks.map((block) => {
          const selected = selectedIndex === block.nodeIndex;
          const inPath = pathIndexes?.has(block.nodeIndex) ?? false;
          return (
            <rect
              key={`layer-${block.nodeIndex}`}
              data-testid="layer-block"
              data-layer-node-index={block.nodeIndex}
              x={block.left}
              y={layerRow.top}
              width={block.width}
              height={layerRow.height}
              rx="6"
              fill="var(--series-2)"
              fillOpacity={selected ? 0.95 : 0.6}
              stroke={selected ? "var(--foreground)" : inPath ? "var(--series-4)" : "var(--card)"}
              strokeWidth={selected ? 2 : inPath ? 1.5 : 1}
              opacity={dimmed && !selected ? 0.12 : 1}
            />
          );
        }),
      )}
    </>
  );
}

export function LayerTrackButtons({
  rows,
  layers,
  labels,
  onSelect,
  onZoom,
  onHover,
}: LayerTrackProps & {
  labels: RunTimelineChartLabels;
  onSelect: (index: number) => void;
  /** P4-1 (#4023): label rides along so double-click focus can name the
   *  block it pushed onto the crumb path. */
  onZoom: (window: TimelineWindowOverride, label: string) => void;
  /** P4-1 (#4023): hover feed for the persistent readout line. */
  onHover?: (nodeIndex: number | null) => void;
}) {
  return (
    <>
      {rows.flatMap((layerRow) =>
        layerRow.blocks.map((block) => {
          const node = layers[block.nodeIndex];
          const firstLine = node.summary.split("\n")[0];
          return (
            <button
              key={`layer-btn-${block.nodeIndex}`}
              type="button"
              aria-label={layerNodeLabel(node, labels)}
              data-testid="layer-block-button"
              data-layer-node-index={block.nodeIndex}
              onClick={() => onSelect(block.nodeIndex)}
              onPointerEnter={onHover ? () => onHover(block.nodeIndex) : undefined}
              onPointerLeave={onHover ? () => onHover(null) : undefined}
              onFocus={onHover ? () => onHover(block.nodeIndex) : undefined}
              onBlur={onHover ? () => onHover(null) : undefined}
              onDoubleClick={() => onZoom({ from: node.start, to: node.end }, layerFocusLabel(node))}
              className="absolute rounded-md outline-none focus-visible:ring-2 focus-visible:ring-foreground focus-visible:ring-offset-2"
              style={{
                left: `${block.left}px`,
                top: `${layerRow.top}px`,
                width: `${block.width}px`,
                height: `${layerRow.height}px`,
              }}
            >
              {block.width >= 40 ? (
                <span
                  data-testid="fixed-timeline-text"
                  className="block truncate px-1 text-[10px] font-medium text-foreground"
                  style={{ left: `${block.left}px` }}
                >
                  {firstLine}
                </span>
              ) : null}
            </button>
          );
        }),
      )}
    </>
  );
}

/** The pending-placeholder geometry: muted, dashed, deliberately not a real
 *  block — uncovered activity reads as "not generated yet" (B spec §3). */
export function PendingTrackGeometry({
  row,
  blocks,
}: {
  row: { top: number; height: number };
  blocks: TimelinePendingSpanLayout[];
}) {
  return (
    <>
      {blocks.map((block) => (
        <rect
          key={`pending-${block.index}`}
          data-testid="pending-block"
          data-pending-index={block.index}
          x={block.left}
          y={row.top}
          width={block.width}
          height={row.height}
          rx="6"
          fill="var(--muted)"
          fillOpacity={0.35}
          stroke="var(--border)"
          strokeDasharray="4 4"
        />
      ))}
    </>
  );
}

/** Clickable shells over the pending geometry: hover/click explains the
 *  stretch, double-click zooms it; never selectable — there is no layer
 *  detail to open (B spec §3). */
export function PendingTrackButtons({
  row,
  blocks,
  labels,
  onShowPopover,
  onHidePopover,
  onZoom,
  describedIndex,
}: {
  row: { top: number; height: number };
  blocks: TimelinePendingSpanLayout[];
  labels: RunTimelineChartLabels;
  onShowPopover: (element: HTMLButtonElement) => void;
  onHidePopover: (event: PointerEvent<HTMLButtonElement> | FocusEvent<HTMLButtonElement>) => void;
  /** P4-1 (#4023): the focus label names the stretch on the crumb path. */
  onZoom: (window: TimelineWindowOverride, label: string) => void;
  describedIndex: number | null;
}) {
  return (
    <>
      {blocks.map((block) => (
        <button
          key={`pending-btn-${block.index}`}
          type="button"
          aria-label={labels.pendingAria}
          aria-describedby={describedIndex === block.index ? TIMELINE_POPOVER_ID : undefined}
          data-testid="pending-block-button"
          data-pending-index={block.index}
          data-timeline-popover-kind="pending"
          data-timeline-popover-index={block.index}
          onPointerEnter={(event) => onShowPopover(event.currentTarget)}
          onPointerLeave={onHidePopover}
          onFocus={(event) => onShowPopover(event.currentTarget)}
          onBlur={onHidePopover}
          onClick={(event) => onShowPopover(event.currentTarget)}
          onDoubleClick={() => onZoom({ from: block.start, to: block.end }, labels.pendingLabel)}
          className="absolute rounded-md outline-none focus-visible:ring-2 focus-visible:ring-foreground focus-visible:ring-offset-2"
          style={{
            left: `${block.left}px`,
            top: `${row.top}px`,
            width: `${block.width}px`,
            height: `${row.height}px`,
          }}
        >
          {block.width >= 40 ? (
            <span
              data-testid="fixed-timeline-text"
              className="block truncate px-1 text-[10px] font-medium text-muted-foreground"
            >
              {labels.pendingLabel}
            </span>
          ) : null}
        </button>
      ))}
    </>
  );
}

export function RawSummaryBand({
  summary,
  labels,
  open,
  onToggle,
}: {
  summary: NonNullable<RunTimelineResponse["summary"]>;
  labels: RunTimelineChartLabels;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <div data-testid="raw-summary" className="rounded-[10px] border border-border bg-muted px-3 py-2">
      <p className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">{labels.layerSummary}</p>
      <p className={cn("mt-1 text-xs leading-5", !open && "line-clamp-2")}>{summary.text}</p>
      <button
        type="button"
        onClick={onToggle}
        className="mt-1 text-[10px] text-muted-foreground hover:text-foreground"
      >
        {open ? labels.showLess : labels.showMore}
      </button>
    </div>
  );
}
