"use client";

// Raw-context strip rendering (P4-2, task #4023): the SVG part rectangles
// drawn inside the chart plus one HTML button per message (aria label +
// interaction), the same split the layer rows use. Geometry comes from
// strip-layout.ts, colors from strip-categories.ts. Bars are clipped at the
// plot edge: with the response window as the axis, an over-extended tail
// (the demo's axis would have grown to fit it) is one zoom away.

import type { RunTimelineMessage } from "@/lib/types";
import { cn } from "@/lib/utils";

import { timestampLabel, type RunTimelineChartLabels } from "./run-timeline-details";
import {
  legendMatches,
  stripMessageClass,
  stripPartClass,
  type StripColorClass,
  type StripLegendCategory,
} from "./strip-categories";
import type { TimelineStripRowLayout } from "./timeline-layout";

const STRIP_CLIP_ID = "run-timeline-strip-clip";

function partOpacity(colorClass: StripColorClass, active: StripLegendCategory | null): number {
  if (active === null) return 1;
  return legendMatches(colorClass, active) ? 1 : 0.12;
}

export function StripTrackGeometry({
  plot,
  row,
  messages,
  selectedIndex,
  relatedIndexes,
  activeCategory,
}: {
  plot: { left: number; width: number };
  row: TimelineStripRowLayout;
  messages: RunTimelineMessage[];
  selectedIndex: number | null;
  relatedIndexes: ReadonlySet<number>;
  activeCategory: StripLegendCategory | null;
}) {
  return (
    <>
      <defs>
        <clipPath id={STRIP_CLIP_ID}>
          <rect x={plot.left} y={row.top} width={plot.width} height={row.height} />
        </clipPath>
      </defs>
      <rect
        data-testid="strip-track"
        x={plot.left}
        y={row.top}
        width={plot.width}
        height={row.height}
        rx="6"
        fill="var(--muted)"
        stroke="var(--border)"
      />
      <g clipPath={`url(#${STRIP_CLIP_ID})`}>
        {row.messages.map((bar, index) => {
          const message = messages[index];
          return (
            <g key={bar.key}>
              {bar.parts.map((part, partIndex) => {
                const colorClass = stripPartClass(part.kind, message.source);
                return (
                  <rect
                    key={partIndex}
                    data-testid="strip-part"
                    data-strip-color={colorClass}
                    x={part.left}
                    y={row.top + 1}
                    width={Math.max(0, part.width)}
                    height={row.height - 2}
                    fill={`var(--strip-${colorClass})`}
                    opacity={partOpacity(colorClass, activeCategory)}
                  />
                );
              })}
            </g>
          );
        })}
        {row.messages.map((bar, index) => {
          const selected = index === selectedIndex;
          const related = !selected && relatedIndexes.has(index);
          if (!selected && !related) return null;
          return (
            <rect
              key={`outline-${bar.key}`}
              data-testid={selected ? "strip-message-selected" : "strip-message-related"}
              data-message-index={index}
              x={bar.left}
              y={row.top}
              width={Math.max(bar.width, 0.75)}
              height={row.height}
              rx="2"
              fill="none"
              stroke={selected ? "var(--foreground)" : "var(--series-4)"}
              strokeWidth={selected ? 2 : 1.5}
            />
          );
        })}
      </g>
    </>
  );
}

export function StripTrackButtons({
  row,
  messages,
  labels,
  onSelect,
  onFocus,
  onHover,
}: {
  row: TimelineStripRowLayout;
  messages: RunTimelineMessage[];
  labels: RunTimelineChartLabels;
  onSelect: (index: number) => void;
  /** Double-click: the chart pushes a crumb and zooms to the message. */
  onFocus: (index: number) => void;
  /** Hover feed for the persistent readout line (single view only). */
  onHover?: (index: number | null) => void;
}) {
  return (
    <>
      {row.messages.map((bar, index) => {
        const message = messages[index];
        const kind = labels.stripPartLabels[stripMessageClass(message)];
        const time = message.ts === null ? labels.none : timestampLabel(message.ts);
        return (
          <button
            key={bar.key}
            type="button"
            data-testid="strip-message-button"
            data-message-index={index}
            aria-label={labels.stripMessageAria(message.idx, kind, time, message.chars.toLocaleString())}
            onClick={() => onSelect(index)}
            onDoubleClick={() => onFocus(index)}
            onPointerEnter={onHover ? () => onHover(index) : undefined}
            onPointerLeave={onHover ? () => onHover(null) : undefined}
            onFocus={onHover ? () => onHover(index) : undefined}
            onBlur={onHover ? () => onHover(null) : undefined}
            className={cn(
              "absolute rounded-[2px] outline-none focus-visible:ring-2 focus-visible:ring-foreground focus-visible:ring-offset-1",
            )}
            style={{
              left: `${bar.left}px`,
              top: `${row.top}px`,
              width: `${Math.max(1, bar.width)}px`,
              height: `${row.height}px`,
            }}
          />
        );
      })}
    </>
  );
}

/** Truncation notice (P4-2 review condition): both the server's budget cap
 *  and its segment-walk cap raise the same flag, so a strip that quietly
 *  misses older messages never reads as complete. */
export function StripTruncatedHint({
  plot,
  row,
  labels,
}: {
  plot: { left: number; width: number };
  row: TimelineStripRowLayout;
  labels: RunTimelineChartLabels;
}) {
  return (
    <span
      data-testid="strip-truncated"
      className="absolute font-mono text-[10px] text-muted-foreground"
      style={{ left: `${plot.left}px`, top: `${row.top + row.height + 2}px` }}
    >
      {labels.stripTruncated}
    </span>
  );
}
