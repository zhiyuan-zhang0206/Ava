// Event-rail presentation helpers for the run timeline: the density cap's
// priority filter and the chip classes. Extracted from run-timeline-chart.tsx
// so the chart stays under the source line budget.

import type { RunTimelineResponse } from "@/lib/types";

// KEEP (task #3696 exception inventory): rail density cap — priority kinds
// first, then the rest, capped at 120 chips; the skipped remainder is
// summarized (`skippedByKind`), not drawn.
const EVENT_RAIL_LIMIT = 120;
// Rail kinds the backend still emits after the task #2591 narrowing: execution
// and halt events no longer appear on the rail (they live in turn rows).
const EVENT_RAIL_PRIORITY = new Set([
  "compact",
  "auto_compact",
  "restart_completed",
  "resurrect",
  "agent_terminated",
  "terminate",
]);

export function eventChipClass(kind: string): string {
  if (kind.includes("failed") || kind.includes("timeout")) {
    return "border-[var(--series-5)] bg-red-50 text-red-700 dark:bg-red-950/30 dark:text-red-400";
  }
  if (kind === "compact" || kind === "auto_compact") {
    return "border-violet-300 bg-violet-50 text-violet-700 dark:bg-violet-950/30 dark:text-violet-400";
  }
  if (kind.includes("restart") || kind.includes("resurrect")) {
    return "border-blue-300 bg-blue-50 text-blue-700 dark:bg-blue-950/30 dark:text-blue-400";
  }
  return "border-border bg-card text-muted-foreground";
}

export function prioritizedRailEvents(events: RunTimelineResponse["events"]) {
  const indexed = events.map((event, index) => ({ event, index }));
  const selected = [
    ...indexed.filter(({ event }) => EVENT_RAIL_PRIORITY.has(event.kind)),
    ...indexed.filter(({ event }) => !EVENT_RAIL_PRIORITY.has(event.kind)),
  ].slice(0, EVENT_RAIL_LIMIT);
  const selectedIndexes = new Set(selected.map(({ index }) => index));
  const skippedByKind = new Map<string, number>();
  for (const { event, index } of indexed) {
    if (!selectedIndexes.has(index)) {
      skippedByKind.set(event.kind, (skippedByKind.get(event.kind) ?? 0) + 1);
    }
  }
  selected.sort((left, right) => Date.parse(left.event.ts) - Date.parse(right.event.ts));
  return {
    events: selected.map(({ event }) => event),
    skippedCount: events.length - selected.length,
    skippedSummary: [...skippedByKind.entries()]
      .map(([kind, count]) => `${kind}×${count}`)
      .join(", "),
  };
}
