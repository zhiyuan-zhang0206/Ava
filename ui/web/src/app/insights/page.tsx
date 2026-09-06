"use client";

// /insights — the cluster's read-only observability surface: live Status and
// the Grafana Ops dashboard link, split out of Control so the two usage
// sessions don't share a page. Insights is meant to sit open and poll Status
// every 15s; its update check runs only on entry or explicit re-check. Control
// is almost entirely static write/manage forms. The standalone Metrics section
// was retired 2026-08-04 — Grafana covers it.
//
// Same shell as Control (app/control/page.tsx): a page header (back to
// conversation + title), a left anchor-nav rail, and ONE scroll container
// holding the sections top to bottom — the outer row clips (overflow-hidden)
// so the document never grows a third scrollbar, and the nav rail + content
// each own their own vertical scroll. The cluster Restart / Update actions ride
// the Status section header (status/page.tsx), so "watch health → act" is one
// glance.

import { MessageSquare } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";

import { useSettledAnchorScroll } from "@/app/control/_anchor-scroll";
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

// Retired 2026-08-24: Resources moved to Grafana and gateway daemons merged
// into Gateway. Keep old bookmarks landing on the closest current surface.
const STATUS_ANCHOR_PREFIX = "status-";
const RETIRED_STATUS_ANCHOR_TARGETS: Record<string, string> = {
  [`${STATUS_ANCHOR_PREFIX}resources`]: "status",
  [`${STATUS_ANCHOR_PREFIX}gateway-daemons`]: "status-gateway",
};

export default function InsightsPage() {
  const t = useTranslations("insights");
  const runTimelineT = useTranslations("runTimeline");
  // Honor a #anchor on first load / direct link (including forwards from old
  // /control#status deep links): resolve the target once, from the URL hash
  // at mount (later hash changes come from nav clicks, which scroll
  // themselves). Retired Metrics anchors (#metrics, #metrics-*) land on the
  // Ops section — the Grafana dashboard link that replaced the Metrics page. The
  // scroll itself is re-applied by useSettledAnchorScroll until the async
  // section bodies stop growing, so the deep link lands on the section at its
  // final position.
  const [anchorTarget] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    const id = window.location.hash.slice(1);
    return id
      ? (RETIRED_STATUS_ANCHOR_TARGETS[id] ?? (RETIRED_INSIGHTS_ANCHORS.has(id) ? "ops" : id))
      : null;
  });
  useSettledAnchorScroll(INSIGHTS_SCROLL_ID, anchorTarget);

  return (
    <div className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header className={cn("shrink-0 items-center gap-1.5 border-b border-border px-4 py-2", FLEX)}>
        <h1 className="text-sm font-semibold">{t("title")}</h1>
        <div className={cn(FLEX_1)} />
        <Link
          href="/"
          className={cn("items-center gap-1.5 rounded px-2 py-1 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground", FLEX)}
          aria-label={t("backToAgents")}
        >
          <MessageSquare className="size-4 shrink-0" aria-hidden />
          <span className="hidden sm:inline">{t("backToAgents")}</span>
        </Link>
      </header>

      {/* overflow-hidden outer boundary: nav rail + content each own their
          vertical scroll, so the document/body never gains a third scrollbar. */}
      <div className={cn("md:flex-row", FLEX, FLEX_COL, FLEX_1, MIN_H_0, OVERFLOW_HIDDEN)}>
        <ControlNav
          sections={INSIGHTS_SECTIONS}
          scrollId={INSIGHTS_SCROLL_ID}
          ariaLabel={t("sections")}
        />
        <div id={INSIGHTS_SCROLL_ID} className={cn("overflow-y-auto overflow-x-hidden", FLEX_1)}>
          <div className={cn("mx-auto max-w-4xl space-y-12 px-6 py-6 font-sans text-sm", MIN_W_0)}>
            <ControlSection
              id="status"
              label={t("statusLabel")}
              description={t("statusDescription")}
            >
              <StatusPage />
            </ControlSection>

            <ControlSection
              id="ops"
              label={t("ops")}
              description={t("opsDescription")}
            >
              <OpsPage />
            </ControlSection>

            <ControlSection
              id="run-timeline"
              label={runTimelineT("insightsEntry")}
              description={runTimelineT("insightsEntryDescription")}
            >
              <RunTimelineEntry />
            </ControlSection>

            <ControlSection
              id="alerts"
              label={t("alerts")}
              description={t("alertsDescription")}
            >
              <AlertsSection />
            </ControlSection>

          </div>
        </div>
      </div>
    </div>
  );
}

function RunTimelineEntry() {
  const t = useTranslations("runTimeline");
  const [agentId, setAgentId] = useState("");
  const parsed = Number(agentId);
  const validAgent = agentId.trim() !== "" && Number.isInteger(parsed) && parsed >= 0;

  return (
    <form className={cn(FLEX, "flex-wrap items-end gap-2")} action={`/insights/run/${validAgent ? parsed : ""}`}>
      <label className="grid gap-1 text-xs text-muted-foreground">
        {t("agentId")}
        <input
          aria-label={t("agentId")}
          type="number"
          min="0"
          value={agentId}
          onChange={(event) => setAgentId(event.target.value)}
          className="w-28 rounded border border-border bg-background px-2 py-1 font-mono text-xs text-foreground"
        />
      </label>
      {validAgent ? (
        <Link href={`/insights/run/${parsed}`} className="rounded bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90">
          {t("open")}
        </Link>
      ) : (
        <button type="submit" disabled className="rounded bg-muted px-2 py-1 text-xs text-muted-foreground">
          {t("open")}
        </button>
      )}
    </form>
  );
}
