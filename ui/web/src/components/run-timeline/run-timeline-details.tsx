import type { PointerEventHandler, RefObject } from "react";

import { FLEX, OVERFLOW_HIDDEN } from "@/lib/layout";
import type { RunTimelineResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

export const TIMELINE_POPOVER_ID = "run-timeline-popover";

export interface RunTimelineChartLabels {
  chart: string;
  visualization: string;
  time: string;
  eventRail: string;
  input: string;
  output: string;
  turn: string;
  bucket: string;
  cost: string;
  model: string;
  empty: string;
  moreEvents: (count: number, summary: string) => string;
  turnDetails: string;
  timeRange: string;
  activeSeconds: string;
  latency: string;
  executions: string;
  tool: string;
  duration: string;
  status: string;
  succeeded: string;
  failed: string;
  anomalies: string;
  none: string;
  noExecutions: string;
  closeDetails: string;
  eventDetails: string;
  kind: string;
  timestamp: string;
  detail: string;
}

export interface TimelinePopoverTarget {
  kind: "turn" | "event";
  index: number;
  left: number;
  top: number;
  width: number;
}

export function rowLabel(
  row: RunTimelineResponse["rows"][number],
  labels: RunTimelineChartLabels,
): string {
  return row.turn === null ? `${labels.bucket} (${row.n_turns})` : `${labels.turn} ${row.turn}`;
}

function currency(amount: number): string {
  return `$${amount.toFixed(amount < 0.01 ? 4 : 2)}`;
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function datePart(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function timePart(date: Date): string {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function timestampLabel(value: string): string {
  const date = new Date(value);
  return `${datePart(date)} ${timePart(date)}`;
}

function timeRange(startValue: string, endValue: string): string {
  const start = new Date(startValue);
  const end = new Date(endValue);
  const endLabel = datePart(start) === datePart(end) ? timePart(end) : timestampLabel(endValue);
  return `${timestampLabel(startValue)} – ${endLabel}`;
}

export function tickLabel(timestamp: string, includeSeconds: boolean): string {
  const date = new Date(timestamp);
  return includeSeconds ? timePart(date) : `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function rowFailed(row: RunTimelineResponse["rows"][number]): boolean {
  return row.ok === false || row.execs.some((execution) => !execution.ok) || row.anomalies.length > 0;
}

function turnFacts(row: RunTimelineResponse["rows"][number], labels: RunTimelineChartLabels) {
  return {
    label: rowLabel(row, labels),
    timeRange: timeRange(row.start, row.end),
    duration: `${row.active_s.toFixed(1)}s`,
    status: rowFailed(row) ? labels.failed : labels.succeeded,
    executions: row.execs.length.toLocaleString(),
    cost: currency(row.llm.cost_usd),
  };
}

function DetailMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-1 rounded-[10px] border border-border bg-muted px-3 py-2">
      <dt className="text-2xs font-medium uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="font-mono text-xs tabular-nums text-foreground">{value}</dd>
    </div>
  );
}

export function TurnDetailPanel({
  row,
  labels,
  onClose,
}: {
  row: RunTimelineResponse["rows"][number];
  labels: RunTimelineChartLabels;
  onClose: () => void;
}) {
  const facts = turnFacts(row, labels);
  return (
    <aside
      role="region"
      aria-label={labels.turnDetails}
      className="h-fit space-y-4 rounded-[10px] border border-border bg-card p-4 text-foreground"
    >
      <header className={cn(FLEX, "items-start justify-between gap-3")}>
        <div>
          <p className="text-2xs font-medium uppercase tracking-wide text-muted-foreground">{labels.turnDetails}</p>
          <h3 className="text-sm font-semibold">{facts.label}</h3>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label={labels.closeDetails}
          className="rounded-md px-1.5 py-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          ×
        </button>
      </header>

      <dl className="grid grid-cols-2 gap-2">
        <div className="col-span-2">
          <DetailMetric label={labels.timeRange} value={facts.timeRange} />
        </div>
        <DetailMetric label={labels.activeSeconds} value={facts.duration} />
        <DetailMetric label={labels.latency} value={`${(row.llm.latency_ms / 1000).toFixed(2)}s`} />
        <DetailMetric label={labels.input} value={row.llm.in_total.toLocaleString()} />
        <DetailMetric label={labels.output} value={row.llm.out_total.toLocaleString()} />
        <DetailMetric label={labels.cost} value={facts.cost} />
        <DetailMetric label={labels.model} value={row.llm.model ?? "—"} />
        <div className="col-span-2">
          <DetailMetric label={labels.status} value={facts.status} />
        </div>
      </dl>

      <section className="space-y-2">
        <h4 className="text-xs font-semibold">{labels.executions}</h4>
        {row.execs.length === 0 ? (
          <p className="text-xs text-muted-foreground">{labels.noExecutions}</p>
        ) : (
          <div className={cn(OVERFLOW_HIDDEN, "rounded-[10px] border border-border")}>
            <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] gap-3 bg-muted px-3 py-2 text-2xs font-medium uppercase tracking-wide text-muted-foreground">
              <span>{labels.tool}</span>
              <span>{labels.duration}</span>
              <span>{labels.status}</span>
            </div>
            {row.execs.map((execution, index) => (
              <div
                key={`${execution.tool}-${index}`}
                className="grid grid-cols-[minmax(0,1fr)_auto_auto] gap-3 border-t border-border px-3 py-2 font-sans text-xs"
              >
                <span className="truncate font-mono">{execution.tool}</span>
                <span className="font-mono tabular-nums">{execution.dur_s.toFixed(2)}s</span>
                <span className={execution.ok ? "text-emerald-700 dark:text-emerald-400" : "text-red-700 dark:text-red-400"}>
                  {execution.ok ? labels.succeeded : labels.failed}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="space-y-2">
        <h4 className="text-xs font-semibold">{labels.anomalies}</h4>
        {row.anomalies.length === 0 ? (
          <p className="text-xs text-muted-foreground">{labels.none}</p>
        ) : (
          <div className={cn(FLEX, "flex-wrap gap-1.5")}>
            {row.anomalies.map((anomaly) => (
              <span
                key={anomaly}
                className="rounded-md border border-red-200 bg-red-50 px-2 py-1 font-mono text-2xs text-red-700 dark:border-red-500/30 dark:bg-red-950/30 dark:text-red-400"
              >
                {anomaly}
              </span>
            ))}
          </div>
        )}
      </section>
    </aside>
  );
}

export function TimelinePopover({
  target,
  row,
  event,
  labels,
  popoverRef,
  onPointerLeave,
}: {
  target: TimelinePopoverTarget;
  row: RunTimelineResponse["rows"][number] | null;
  event: RunTimelineResponse["events"][number] | null;
  labels: RunTimelineChartLabels;
  popoverRef: RefObject<HTMLDivElement | null>;
  onPointerLeave: PointerEventHandler<HTMLDivElement>;
}) {
  const facts = row ? turnFacts(row, labels) : null;
  return (
    <div
      ref={popoverRef}
      id={TIMELINE_POPOVER_ID}
      role="tooltip"
      className="absolute z-20 max-h-[min(24rem,calc(100vh-2rem))] overflow-y-auto rounded-lg border border-border bg-popover p-3 text-popover-foreground shadow-xl"
      style={{ left: target.left, top: target.top, width: target.width }}
      onPointerLeave={onPointerLeave}
    >
      {facts ? (
        <>
          <p className="text-xs font-semibold">{facts.label}</p>
          <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
            <dt className="text-muted-foreground">{labels.timeRange}</dt>
            <dd className="font-mono tabular-nums">{facts.timeRange}</dd>
            <dt className="text-muted-foreground">{labels.activeSeconds}</dt>
            <dd className="font-mono tabular-nums">{facts.duration}</dd>
            <dt className="text-muted-foreground">{labels.status}</dt>
            <dd>{facts.status}</dd>
            <dt className="text-muted-foreground">{labels.executions}</dt>
            <dd className="font-mono tabular-nums">{facts.executions}</dd>
            <dt className="text-muted-foreground">{labels.cost}</dt>
            <dd className="font-mono tabular-nums">{facts.cost}</dd>
          </dl>
        </>
      ) : event ? (
        <>
          <p className="text-xs font-semibold">{labels.eventDetails}</p>
          <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
            <dt className="text-muted-foreground">{labels.kind}</dt>
            <dd className="font-mono">{event.kind}</dd>
            <dt className="text-muted-foreground">{labels.timestamp}</dt>
            <dd className="font-mono tabular-nums">{timestampLabel(event.ts)}</dd>
            <dt className="text-muted-foreground">{labels.detail}</dt>
            <dd className="whitespace-pre-wrap break-words font-mono">{event.label ?? labels.none}</dd>
          </dl>
        </>
      ) : null}
    </div>
  );
}
