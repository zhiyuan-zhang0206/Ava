"use client";

// /insights#ops — the Ops Monitor panel as an embedded Grafana dashboard
// (user pivot 2026-08-03: the self-made SVG panel from #672 is replaced by a
// Grafana single-iframe embed (the ops dashboard is the only iframe).
//
// The iframe loads the "Ava Ops" dashboard (dashboards/ops/ava-ops-main.json)
// through the gateway's /grafana reverse proxy — same origin family as
// API_BASE, auth-gated by the cluster middleware like every other route, so
// the browser's session cookie applies. The theme follows next-themes;
// `kiosk` hides Grafana's sidemenu/topnav but keeps the dashboard's own
// timepicker, so the time range and refresh interval are Grafana's native
// controls (the frontend window selector + Refresh were removed 2026-08-05
// per user ruling — the iframe URL carries no from/to, the dashboard's
// defaults apply). autofitpanels stays off: on Grafana 13.1.1 it collapses
// every panel to a 30px title bar at embed widths (see
// dashboards/ops/README.md).
//
// The frame is full-height, no inner scrollbar: EMBED_HEIGHT equals the
// dashboard's fixed rendered height (panels are gridPos-sized, so the
// height is a constant regardless of window width or time range). Measured
// on Grafana 13.1.1 — 124 grid rows × 30px + 123 gaps × 8px + 116px chrome =
// 4820px for ava-ops-main (2026-08-06: core + plugin metrics are rendered
// into this one dashboard by scripts/gen_plugin_dashboard.py — core section
// first, then one row per plugin — so no separate plugins embed exists).
// Keep in sync with the gridPos layout — recompute with the formula above
// when panels change.
//
// The self-made chart components (components/ops/charts.tsx) and the
// /api/ops/monitor polling were retired with this rewrite; the backend
// endpoint and the event-stream instrumentation stay (Grafana queries the
// same stream via its Postgres datasource).

import { useTheme } from "next-themes";

import { API_BASE } from "@/lib/api";
import { OVERFLOW_HIDDEN } from "@/lib/layout";
import { cn } from "@/lib/utils";

// Fixed uid of the embedded dashboard (dashboards/ops/ava-ops-main.json) —
// the gateway reverse proxy maps /grafana/d/<uid> onto the Grafana instance.
// Core metrics (row header "core") + plugin metrics (one row per plugin) are
// rendered into this one dashboard by scripts/gen_plugin_dashboard.py (core
// panels ids < 1000, plugin panels ids >= 1000).
const DASHBOARD_UID = "ava-ops-main";

// Full-height embed (see header comment): 138 grid rows → 5352px on
// Grafana 13.1.1 (4 frontend-telemetry panels added 2026-08-09). Bump when
// dashboards/ops/ava-ops-main.json panels change — page.test.tsx derives the
// expected value from the dashboard's gridPos and fails when the two drift
// apart.
export const EMBED_HEIGHT = 5770;

export default function OpsPage() {
  const { resolvedTheme } = useTheme();
  const theme = resolvedTheme === "dark" ? "dark" : "light";
  const src = `${API_BASE}/grafana/d/${DASHBOARD_UID}?theme=${theme}&kiosk`;

  return (
    <div className="space-y-4">
      {/* Metrics (Grafana): the embedded Ava Ops dashboard — the section's
          first sub-heading (nav anchor ops-metrics). */}
      <div id="ops-metrics" className="scroll-mt-4">
        <h3 className="mb-2 text-sm font-semibold text-muted-foreground">
          Metrics (Grafana)
        </h3>
        {/* Full-height frame: panels render at natural size; the page scrolls
            and the iframe has no inner scrollbar. */}
        <div className={cn("rounded-md border border-border", OVERFLOW_HIDDEN)}>
          <iframe
            data-testid="ops-embed-frame"
            title="Ava ops"
            src={src}
            style={{ height: EMBED_HEIGHT }}
            className="w-full"
          />
        </div>
      </div>

    </div>
  );
}
