"use client";

// Category legend for the raw-context strip (P4-2, task #4023): the demo's
// nine rows, swatch + name, click to highlight every matching part (muted
// 0.12 otherwise), click again to clear. Rendered below the chart; the
// compare view does not show it (M7 — compare density is not designed yet).

import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

import type { RunTimelineChartLabels } from "./run-timeline-details";
import {
  STRIP_LEGEND_CATEGORIES,
  type StripColorClass,
  type StripLegendCategory,
} from "./strip-categories";

/** The swatch color of each legend row; the sys row stands on the compact
 *  color the demo uses for its "compact / system" marker. */
const LEGEND_SWATCH: Record<StripLegendCategory, StripColorClass> = {
  think: "think",
  text: "text",
  call: "call",
  out: "out",
  note: "note",
  "ib-agent": "ib-agent",
  "ib-user": "ib-user",
  sys: "compact",
  prompt: "prompt",
};

export function StripLegend({
  active,
  labels,
  onToggle,
}: {
  active: StripLegendCategory | null;
  labels: RunTimelineChartLabels;
  onToggle: (category: StripLegendCategory) => void;
}) {
  return (
    <div className={cn(FLEX, "flex-wrap items-center gap-x-3 gap-y-1 px-1 pt-1")}>
      <ul aria-label={labels.legendTitle} className={cn(FLEX, "flex-wrap items-center gap-x-3 gap-y-1")}>
        {STRIP_LEGEND_CATEGORIES.map((category) => (
          <li key={category}>
            <button
              type="button"
              aria-pressed={active === category}
              title={labels.legendHint}
              onClick={() => onToggle(category)}
              className={cn(
                cn(FLEX, "items-center gap-1.5 rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-foreground focus-visible:ring-offset-2"),
                active === category && "bg-primary/10 text-foreground ring-1 ring-primary/40",
              )}
            >
              <span
                aria-hidden="true"
                className="inline-block size-2.5 shrink-0 rounded-[3px]"
                style={{ background: `var(--strip-${LEGEND_SWATCH[category]})` }}
              />
              {labels.legendLabels[category]}
            </button>
          </li>
        ))}
      </ul>
      <span className="text-[10px] text-muted-foreground/80">{labels.legendHint}</span>
    </div>
  );
}
