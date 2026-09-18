"use client";

// Persistent hover readout for the single run view (P4-1, task #4023): the
// demo's readout line above the chart, naming the hovered block. The line
// truncates with an ellipsis and sits outside the chart body, so hover info
// never covers the track; full detail stays in the popover and the panel.

import type { RunTimelineMessage, RunTimelineResponse } from "@/lib/types";

import { rowLabel, type RunTimelineChartLabels } from "./run-timeline-details";
import { stripMessageClass } from "./strip-categories";

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function clock(date: Date): string {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** One point stamp for the readout line (P4-2): short date + clock. */
function point(date: Date): string {
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${clock(date)}`;
}

function range(from: string, to: string): string {
  const start = new Date(from);
  const end = new Date(to);
  if (start.toDateString() === end.toDateString()) {
    return `${clock(start)} – ${clock(end)}`;
  }
  const stamp = (date: Date) => `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${clock(date)}`;
  return `${stamp(start)} – ${stamp(end)}`;
}

export function buildReadoutText(
  input: {
    row?: RunTimelineResponse["rows"][number] | null;
    event?: RunTimelineResponse["events"][number] | null;
    layer?: NonNullable<RunTimelineResponse["layers"]>[number] | null;
    pending?: { start: string; end: string } | null;
    /** P4-2 (#4023): hovered raw-context message + its covering summary leaf. */
    message?: { message: RunTimelineMessage; leaf: string | null } | null;
  },
  labels: RunTimelineChartLabels,
): string | null {
  if (input.message) {
    const { message, leaf } = input.message;
    return labels.readoutMessage(
      message.idx,
      labels.stripPartLabels[stripMessageClass(message)],
      message.ts === null ? labels.none : point(new Date(message.ts)),
      message.chars.toLocaleString(),
      leaf,
    );
  }
  if (input.row) {
    const row = input.row;
    return `${rowLabel(row, labels)} · ${range(row.start, row.end)} · ${row.execs.length} ${labels.executions} · $${row.llm.cost_usd.toFixed(2)}`;
  }
  if (input.layer) {
    const layer = input.layer;
    const firstLine = layer.summary.split("\n")[0];
    return `L${layer.depth}#${layer.id} · ${range(layer.start, layer.end)} · ${firstLine}`;
  }
  if (input.pending) {
    return `${labels.pendingLabel} · ${range(input.pending.start, input.pending.end)}`;
  }
  if (input.event) {
    return `${input.event.kind} · ${clock(new Date(input.event.ts))}`;
  }
  return null;
}

export function TimelineReadout({
  text,
  labels,
}: {
  text: string | null;
  labels: RunTimelineChartLabels;
}) {
  return (
    <p
      data-testid="timeline-readout"
      title={text ?? undefined}
      className="truncate rounded border border-border/70 bg-muted/40 px-2 py-0.5 font-mono text-[11px] leading-5 text-muted-foreground"
    >
      {text ?? labels.readoutIdle}
    </p>
  );
}
