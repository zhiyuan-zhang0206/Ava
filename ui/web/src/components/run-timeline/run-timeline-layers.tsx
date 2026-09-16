"use client";

// Narrative-layer rendering for the run timeline: the layer-block geometry
// drawn inside the chart SVG, the matching clickable overlay buttons, and the
// raw-context summary band. Extracted from run-timeline-chart.tsx to keep that
// file under the source budget.

import type { RunTimelineResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

import { layerNodeLabel, type RunTimelineChartLabels } from "./run-timeline-details";
import type { TimelineWindowOverride } from "./request-level";
import type { TimelineLayerRowLayout } from "./timeline-layout";

interface LayerTrackProps {
  rows: TimelineLayerRowLayout[];
  layers: NonNullable<RunTimelineResponse["layers"]>;
}

export function LayerTrackGeometry({
  rows,
  selectedIndex,
}: {
  rows: TimelineLayerRowLayout[];
  selectedIndex: number | null;
}) {
  return (
    <>
      {rows.flatMap((layerRow) =>
        layerRow.blocks.map((block) => {
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
              fillOpacity={selectedIndex === block.nodeIndex ? 0.95 : 0.6}
              stroke={selectedIndex === block.nodeIndex ? "var(--foreground)" : "var(--card)"}
              strokeWidth={selectedIndex === block.nodeIndex ? 2 : 1}
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
}: LayerTrackProps & {
  labels: RunTimelineChartLabels;
  onSelect: (index: number) => void;
  onZoom: (window: TimelineWindowOverride) => void;
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
              onDoubleClick={() => onZoom({ from: node.start, to: node.end })}
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
