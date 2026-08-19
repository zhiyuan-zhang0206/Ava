"use client";

import type { ResourceSample } from "@/lib/types";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// ResourceReadout — one machine's CPU / memory / disk, right now.
//
// This used to be a sparkline over a 300-sample ring buffer the backend kept
// per process. Since issue #46 the host time series lives in Prometheus
// (scraped by the per-machine OTel Collector sidecar) and the trend is read in
// Grafana; the wire carries a single live sample so the status page still says
// something true on a deployment whose LGTM backend is down or absent. A chart
// of one point is a number, so it is drawn as one.
// ---------------------------------------------------------------------------

function formatGb(n: number): string {
  return n < 10 ? n.toFixed(1) : String(Math.round(n));
}

function Metric({
  label,
  pct,
  detail,
}: {
  label: string;
  pct: number;
  detail?: string;
}) {
  return (
    <div className={cn("items-baseline gap-1.5", FLEX)}>
      <span className="text-muted-foreground w-12 shrink-0">{label}</span>
      <span className="tabular-nums font-medium whitespace-nowrap">{Math.round(pct)}%</span>
      {detail && (
        <span className="text-muted-foreground/70 tabular-nums whitespace-nowrap">{detail}</span>
      )}
    </div>
  );
}

export function ResourceReadout({ sample }: { sample: ResourceSample }) {
  return (
    <div className={cn("flex-nowrap items-center gap-5 overflow-x-auto text-xs", FLEX)}>
      <Metric label="CPU" pct={sample.cpu_pct} />
      <Metric
        label="Memory"
        pct={sample.mem_pct}
        detail={`${formatGb(sample.mem_used_gb)}/${formatGb(sample.mem_total_gb)}GB`}
      />
      <Metric
        label="Disk"
        pct={sample.disk_pct}
        detail={`${formatGb(sample.disk_used_gb)}/${formatGb(sample.disk_total_gb)}GB`}
      />
    </div>
  );
}
