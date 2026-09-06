"use client";

// The alert section (/insights#alerts) — the system→human alert history list
// (Task #1224, user design: the badge jumps here). Unresolved alerts are
// listed first (the backend owns ordering; SSE frames prepend). Alert is fully
// separate from Notice.

import { Loader2 } from "lucide-react";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatDuration } from "@/lib/item-summary";
import { FLEX, MIN_W_0 } from "@/lib/layout";
import { formatRelativeTime } from "@/lib/sidebar";
import type { Alert, AlertSeverity, AlertStatus } from "@/lib/types";
import { useAlertsSection } from "@/lib/use-alerts";
import { cn } from "@/lib/utils";

// Unresolved severity pill fill — critical = destructive, error = orange,
// warning = yellow. Resolved pills use muted tokens below; the word stays
// visible, so color is never the only signal.
const SEVERITY_BG: Record<AlertSeverity, string> = {
  critical: "bg-destructive",
  error: "bg-orange-500",
  warning: "bg-yellow-500",
};

// Status — colors with a text label beside them (never color alone).
// unresolved = live problem (red); resolved = ended, informational (muted).
const STATE_DOT: Record<AlertStatus, string> = {
  unresolved: "bg-destructive",
  resolved: "bg-muted-foreground/50",
};
const STATE_TEXT: Record<AlertStatus, string> = {
  unresolved: "text-destructive",
  resolved: "text-muted-foreground",
};

// The API stores starts_at/ends_at — duration is derived, not a column:
// elapsed while still unresolved (ends_at null), total once resolved. Clamped
// at zero so a clock skew can never render a negative duration.
function alertDurationMs(a: Alert): number {
  const start = new Date(a.starts_at).getTime();
  const end = a.ends_at ? new Date(a.ends_at).getTime() : Date.now();
  return Math.max(0, end - start);
}

function summaryOf(a: Alert): string {
  return a.annotations.summary || "";
}

export default function AlertsSection() {
  const { data, isLoading, error } = useAlertsSection();

  const alerts = data?.alerts ?? [];

  return (
    <div data-testid="alerts-section" className="rounded-md border border-border">
      {isLoading ? (
        <div className={cn("justify-center py-8", FLEX)}>
          <Loader2 className="size-5 animate-spin text-muted-foreground" />
        </div>
      ) : error ? (
        <div className="px-3 py-4 text-center text-sm text-muted-foreground">
          Couldn&apos;t load alerts — retrying…
        </div>
      ) : alerts.length === 0 ? (
        <div className="px-3 py-4 text-center text-sm text-muted-foreground">
          No alerts in the last 24h
        </div>
      ) : (
        // No horizontal scrolling at any viewport width (task #1960): the
        // table is fixed-layout so the declared column widths hold and the
        // Alert column (the variable one) wraps its content instead of
        // growing the table past the container. The Started / Duration /
        // Source columns hide below lg — a 390px phone keeps Severity, Alert,
        // and State, which fit with the detail cell wrapping (md-to-lg tablets
        // keep the same three columns — six fixed columns squeezed the
        // Alert column to ~30px at iPad-portrait widths, QA #989). The shadcn
        // Table's own overflow-x-auto container stays as a defensive net; with
        // every cell wrapping it never overflows.
        <Table className="table-fixed">
          <TableHeader>
            <TableRow>
              <TableHead className="w-20">Severity</TableHead>
              <TableHead className={MIN_W_0}>Alert</TableHead>
              <TableHead className="hidden w-28 lg:table-cell">Started</TableHead>
              <TableHead className="hidden w-20 lg:table-cell">Duration</TableHead>
              <TableHead className="w-24">State</TableHead>
              <TableHead className="hidden w-24 lg:table-cell">Source</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {alerts.map((a) => (
              <TableRow key={a.id} data-testid={`alert-row-${a.id}`}>
                <TableCell>
                  <span
                    className={cn(
                      "inline-flex items-center rounded px-1.5 py-0.5 text-2xs font-semibold",
                      a.status === "resolved"
                        ? "bg-muted text-muted-foreground"
                        : cn("text-white", SEVERITY_BG[a.severity]),
                    )}
                  >
                    {a.severity}
                  </span>
                </TableCell>
                <TableCell className={MIN_W_0}>
                  {/* Multi-line wrapping detail cell: whitespace-normal +
                      break-words override the shared TableCell nowrap so long
                      alert names and annotations wrap to further lines instead
                      of forcing a horizontal scroll. The line-clamp-2 was
                      dropped — the details must read fully, not ellipsize
                      (task #1960). */}
                  <div className="whitespace-normal break-words font-mono text-xs font-semibold">
                    {a.alertname}
                  </div>
                  {summaryOf(a) && (
                    <div className="mt-0.5 whitespace-normal break-words text-xs text-muted-foreground">
                      {summaryOf(a)}
                    </div>
                  )}
                </TableCell>
                <TableCell className="hidden lg:table-cell">
                  <span className="tabular-nums" title={new Date(a.starts_at).toLocaleString()}>
                    {formatRelativeTime(a.starts_at)}
                  </span>
                </TableCell>
                <TableCell className="hidden tabular-nums text-muted-foreground lg:table-cell">
                  {formatDuration(alertDurationMs(a))}
                </TableCell>
                <TableCell>
                  <span className={cn("inline-flex items-center gap-1.5 text-xs", STATE_TEXT[a.status])}>
                    <span className={cn("size-1.5 rounded-full", STATE_DOT[a.status])} />
                    {a.status}
                  </span>
                </TableCell>
                <TableCell className="hidden text-xs text-muted-foreground lg:table-cell">
                  {a.source}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
