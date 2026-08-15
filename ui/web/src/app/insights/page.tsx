"use client";

// /insights — the cluster's read-only observability surface: live Status and
// the embedded Grafana Ops dashboard, split out of Control so the two usage
// sessions don't share a page. Insights is meant to sit open and poll (Status
// every 10s, its update-check every 30s); Control is almost entirely static
// write/manage forms. The standalone Metrics section was retired 2026-08-04 —
// Grafana covers it.
//
// Same shell as Control (app/control/page.tsx): a page header (back to
// conversation + title), a left anchor-nav rail, and ONE scroll container
// holding the sections top to bottom — the outer row clips (overflow-hidden)
// so the document never grows a third scrollbar, and the nav rail + content
// each own their own vertical scroll. The cluster Restart / Update actions ride
// the Status section header (status/page.tsx), so "watch health → act" is one
// glance.

import { MessageSquare } from "lucide-react";
import Link from "next/link";
import { useEffect } from "react";

import { ControlNav } from "@/app/control/_nav";
import {
  INSIGHTS_SCROLL_ID,
  INSIGHTS_SECTIONS,
  RETIRED_INSIGHTS_ANCHORS,
} from "@/app/control/_sections";
import { ControlSection } from "@/app/control/_section";
import OpsPage from "@/app/insights/ops/page";
import AlertsSection from "@/components/ops/alerts-section";
import StatusPage from "@/app/insights/status/page";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout";
import { cn } from "@/lib/utils";

export default function InsightsPage() {
  // Honor a #anchor on first load / direct link (including forwards from old
  // /control#status deep links): scroll the target into the container once the
  // sections mount. Retired Metrics anchors (#metrics, #metrics-*) land on the
  // Ops section — the Grafana embed that replaced the Metrics page.
  useEffect(() => {
    const id = window.location.hash.slice(1);
    if (!id) return;
    const target = RETIRED_INSIGHTS_ANCHORS.has(id) ? "ops" : id;
    const el = document.getElementById(target);
    if (el) el.scrollIntoView({ block: "start" });
  }, []);

  return (
    <div className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header className={cn("shrink-0 items-center gap-1.5 border-b border-border px-4 py-2", FLEX)}>
        <h1 className="text-sm font-semibold">Insights</h1>
        <div className={cn(FLEX_1)} />
        <Link
          href="/"
          className={cn("items-center gap-1.5 rounded px-2 py-1 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground", FLEX)}
          aria-label="Back to agents"
        >
          <MessageSquare className="size-4 shrink-0" aria-hidden />
          <span className="hidden sm:inline">Back to agents</span>
        </Link>
      </header>

      {/* overflow-hidden outer boundary: nav rail + content each own their
          vertical scroll, so the document/body never gains a third scrollbar. */}
      <div className={cn("md:flex-row", FLEX, FLEX_COL, FLEX_1, MIN_H_0, OVERFLOW_HIDDEN)}>
        <ControlNav
          sections={INSIGHTS_SECTIONS}
          scrollId={INSIGHTS_SCROLL_ID}
          ariaLabel="Insights sections"
        />
        <div id={INSIGHTS_SCROLL_ID} className={cn("overflow-y-auto overflow-x-hidden", FLEX_1)}>
          <div className={cn("mx-auto max-w-4xl space-y-12 px-6 py-6 font-mono text-sm", MIN_W_0)}>
            <ControlSection
              id="status"
              label="Status"
              description="Live cluster health: services, agents, resources."
            >
              <StatusPage />
            </ControlSection>

            <ControlSection
              id="ops"
              label="Ops"
              description="Embedded Grafana dashboard — SSE/event-log backlog, LLM latency + TPS, process restarts."
            >
              <OpsPage />
            </ControlSection>

            <ControlSection
              id="alerts"
              label="Alerts"
              description="System alerts — separate from notices. Unresolved-first history, live via SSE."
            >
              <AlertsSection />
            </ControlSection>

          </div>
        </div>
      </div>
    </div>
  );
}
