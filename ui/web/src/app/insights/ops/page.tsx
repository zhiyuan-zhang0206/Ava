"use client";

// /insights#ops — the full-height Grafana iframe was retired by user ruling
// 2026-08-23. This section is now one link to the merged Ava Ops dashboard
// through the gateway's /grafana proxy.

import { API_BASE } from "@/lib/api";

const OPS_DASHBOARD_URL = `${API_BASE}/grafana/d/ava-ops-main?from=now-6h&to=now`;

export default function OpsPage() {
  return (
    <div className="space-y-4">
      <div id="ops-metrics" className="scroll-mt-4">
        <h3 className="mb-2 text-sm font-semibold text-muted-foreground">
          Metrics (Grafana)
        </h3>
        <a
          href={OPS_DASHBOARD_URL}
          target="_blank"
          rel="noreferrer"
          className="text-xs text-muted-foreground hover:text-foreground underline underline-offset-2"
        >
          Open the Ava ops dashboard
        </a>
      </div>
    </div>
  );
}
