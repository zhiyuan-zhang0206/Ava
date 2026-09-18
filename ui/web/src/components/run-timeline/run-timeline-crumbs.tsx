"use client";

// Focus-crumb bar for the single run view (P4-1, task #4023): the focus path
// the demo shows above its chart. The root returns to the initial window;
// each entry restores the range captured when the block was focused. The
// compare view does not pass entries, so it renders no bar.

import { FLEX, MIN_W_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

import type { RunTimelineChartLabels } from "./run-timeline-details";

export interface TimelineCrumbEntry {
  label: string;
  from: string;
  to: string;
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function shortRange(from: string, to: string): string {
  const start = new Date(from);
  const end = new Date(to);
  const hm = (date: Date) => `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return start.toDateString() === end.toDateString()
    ? `${hm(start)}–${hm(end)}`
    : `${pad(start.getMonth() + 1)}-${pad(start.getDate())} ${hm(start)}–${pad(end.getMonth() + 1)}-${pad(end.getDate())} ${hm(end)}`;
}

export function TimelineCrumbs({
  entries,
  labels,
  onSelect,
}: {
  entries: TimelineCrumbEntry[];
  labels: RunTimelineChartLabels;
  onSelect?: (index: number) => void;
}) {
  if (entries.length === 0) return null;
  return (
    <nav
      data-testid="timeline-crumbs"
      className={cn(
        FLEX,
        "flex-wrap items-center gap-x-1 gap-y-0.5 px-1 text-[11px] leading-5 text-muted-foreground",
      )}
    >
      <button
        type="button"
        onClick={() => onSelect?.(-1)}
        className="max-w-[10rem] truncate rounded px-1 hover:bg-muted hover:text-foreground"
      >
        {labels.crumbRoot}
      </button>
      {entries.map((entry, index) => (
        <span key={`${entry.from}-${entry.label}`} className={cn(FLEX, MIN_W_0, "items-center gap-1")}>
          <span aria-hidden="true" className="text-muted-foreground/60">
            ›
          </span>
          <button
            type="button"
            onClick={() => onSelect?.(index)}
            className={cn(
              "max-w-[18rem] truncate rounded px-1 hover:bg-muted hover:text-foreground",
              index === entries.length - 1 && "bg-muted/60 font-medium text-foreground",
            )}
          >
            {entry.label}{" "}
            <span className="font-mono text-[10px] tabular-nums">
              {shortRange(entry.from, entry.to)}
            </span>
          </button>
        </span>
      ))}
    </nav>
  );
}
