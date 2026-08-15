"use client";

// The alert section (/insights#alerts) — the system→human alert history list
// (Task #1224, user design: timeline bar + badge jump here). Unresolved
// first, then unread, then newest start (the backend's order; SSE frames
// prepend). Visiting the section auto-marks everything read — the badge
// clears and unread rows dim. Alert is fully separate from Notice.

import { useEffect } from "react";

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
import { FLEX } from "@/lib/layout";
import { formatRelativeTime } from "@/lib/sidebar";
import type { Alert, AlertSeverity, AlertStatus } from "@/lib/types";
import { useAlertsSection, useMarkAllAlertsRead } from "@/lib/use-alerts";
import { cn } from "@/lib/utils";

import { useSectionVisible } from "@/app/control/_visibility";

// Severity pill fill — critical = destructive, error = orange, warning =
// yellow. Text is always white and the word is inside the pill, so color is
// never the only signal.
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
  const visible = useSectionVisible();
  const { data, isLoading, error } = useAlertsSection();
  const markAllRead = useMarkAllAlertsRead();
  // Auto-mark-read: while the section is visible, any unread rows clear
  // immediately (the badge goes with them). The mutation is idempotent and
  // the onSuccess cache patch drops unread_count to 0, so this only fires
  // again when a NEW unread alert lands while the section stays open.
  useEffect(() => {
    if (!visible) return;
    const unread = data?.meta.unread_count ?? 0;
    // isPending guard: mutating flips isPending (a new effect pass) BEFORE
    // the onSuccess cache patch lands — without the guard the same unread
    // batch would fire twice.
    if (unread > 0 && !markAllRead.isPending) markAllRead.mutate();
  }, [visible, data, markAllRead]);

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
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-20">Severity</TableHead>
                <TableHead>Alert</TableHead>
                <TableHead className="w-28">Started</TableHead>
                <TableHead className="w-20">Duration</TableHead>
                <TableHead className="w-24">State</TableHead>
                <TableHead className="w-24">Source</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {alerts.map((a) => (
                <TableRow
                  key={a.id}
                  className={cn(a.read_at === null && "bg-accent/40")}
                  data-testid={`alert-row-${a.id}`}
                >
                  <TableCell>
                    <span
                      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold text-white ${SEVERITY_BG[a.severity]}`}
                    >
                      {a.severity}
                    </span>
                  </TableCell>
                  <TableCell>
                    <div className="font-mono text-xs font-semibold">{a.alertname}</div>
                    {summaryOf(a) && (
                      <div className="mt-0.5 line-clamp-2 text-[11px] text-muted-foreground">
                        {summaryOf(a)}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <span className="tabular-nums" title={new Date(a.starts_at).toLocaleString()}>
                      {formatRelativeTime(a.starts_at)}
                    </span>
                  </TableCell>
                  <TableCell className="tabular-nums text-muted-foreground">
                    {formatDuration(alertDurationMs(a))}
                  </TableCell>
                  <TableCell>
                    <span className={cn("inline-flex items-center gap-1.5 text-xs", STATE_TEXT[a.status])}>
                      <span className={cn("size-1.5 rounded-full", STATE_DOT[a.status])} />
                      {a.status}
                    </span>
                  </TableCell>
                  <TableCell className="text-[11px] text-muted-foreground">{a.source}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
