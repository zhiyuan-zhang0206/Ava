"use client";

// /control — the cluster's write/manage surface: a page header (back to
// conversation + title), a left anchor-nav rail, and a single scroll container
// holding every section top to bottom (Guide, Config, Presets, Display,
// Plugins, MCP, Skills, Schedules, OKF Graph). The read-only observability sections
// (Status + Ops + Alerts) live on their own page, /insights — see app/insights.
// The horizontal TabBar is long gone: the whole page is Ctrl-F-searchable and
// every section is deep-linkable (#config-gateway, #skills, …). Named Control,
// not Settings — it carries operational actions (schedules, config, inventory),
// not just preferences.
//
// Every section's scaffolding AND its data queries render/fetch from first
// paint — see _visibility.tsx for how that stays within the connection budget
// as the user scrolls. The section bodies below are the existing per-section
// components, unchanged except for threading `enabled: visible` into their
// queries.

import { ExternalLink, MessageSquare } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useSettledAnchorScroll } from "./_anchor-scroll";

import ConfigPage from "@/app/control/config/page";
import DisplaySettingsPage from "@/app/control/display/page";
import GuidePage from "@/app/control/guide/page";
import { McpInventory, PluginsInventory } from "@/app/control/inventory/page";
import PresetsPage from "@/app/control/presets/page";
import SchedulesPage from "@/app/control/schedules/page";
import SkillsPage from "@/app/control/skills/page";
import { PluginNavList } from "@/components/plugin-nav";
import { buttonVariants } from "@/components/ui/button";
import { assetUrl } from "@/lib/api";

import { ControlNav } from "./_nav";
import {
  CONTROL_SCROLL_ID,
  CONTROL_SECTIONS,
  controlAnchorIds,
  INSIGHTS_SECTIONS,
  RETIRED_INSIGHTS_ANCHORS,
} from "./_sections";
import { ControlSection } from "./_section";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout";
import { cn } from "@/lib/utils";

// Anchors that used to live on Control but moved to /insights (status, ops).
// Old /control#status / #ops deep links forward there so they land on the
// right section.
const INSIGHTS_ANCHORS = new Set(controlAnchorIds(INSIGHTS_SECTIONS));

// Retired Metrics anchors (2026-08-04): the Metrics page moved to Grafana, so
// old /control#metrics / #metrics-* deep links land on the Ops section, which
// links to the dashboard that replaced them.
const RETIRED_ANCHOR_TARGET = "ops";

export default function ControlPage() {
  const t = useTranslations("control");
  const ts = useTranslations("control.sections");
  const router = useRouter();
  // Honor a #anchor on first load / direct link: resolve the target once,
  // from the URL hash at mount (later hash changes come from nav clicks,
  // which scroll themselves). Anchors that migrated to /insights redirect
  // there; the rest scroll their target into the container — no smooth, it's
  // the initial position, re-applied by useSettledAnchorScroll while the
  // async section bodies settle.
  const [anchorTarget] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    const id = window.location.hash.slice(1);
    if (!id || INSIGHTS_ANCHORS.has(id) || RETIRED_INSIGHTS_ANCHORS.has(id)) return null;
    return id;
  });
  useEffect(() => {
    const id = window.location.hash.slice(1);
    if (!id) return;
    if (INSIGHTS_ANCHORS.has(id)) {
      router.replace(`/insights#${id}`);
      return;
    }
    if (RETIRED_INSIGHTS_ANCHORS.has(id)) {
      router.replace(`/insights#${RETIRED_ANCHOR_TARGET}`);
      return;
    }
  }, [router]);
  useSettledAnchorScroll(CONTROL_SCROLL_ID, anchorTarget);

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

      {/* overflow-hidden here is the outer boundary: the two children (nav rail +
          content) each own their vertical scroll, so nothing should overflow this
          row — clipping it guarantees the document/body itself never gains a third
          scrollbar even if a child mis-sizes. */}
      <div className={cn("md:flex-row", FLEX, FLEX_COL, FLEX_1, MIN_H_0, OVERFLOW_HIDDEN)}>
        <ControlNav
          sections={CONTROL_SECTIONS}
          scrollId={CONTROL_SCROLL_ID}
          ariaLabel="Control sections"
        />
        <div id={CONTROL_SCROLL_ID} className={cn("overflow-y-auto overflow-x-hidden", FLEX_1)}>
          <div className={cn("mx-auto max-w-6xl space-y-12 px-6 py-6 text-sm", MIN_W_0)}>
            <ControlSection
              id="guide"
              label={ts("guide")}
              description={t("guideDescription")}
            >
              <GuidePage />
            </ControlSection>

            <ControlSection id="config" label={ts("config")}>
              <ConfigPage />
            </ControlSection>

            <ControlSection
              id="presets"
              label={ts("presets")}
              description={t("presetsDescription")}
            >
              <PresetsPage />
            </ControlSection>

            <ControlSection
              id="display"
              label={ts("display")}
              description={t("displayDescription")}
            >
              <DisplaySettingsPage />
            </ControlSection>

            <ControlSection
              id="plugins"
              label={ts("plugins")}
              description={t("pluginsDescription")}
            >
              <PluginsInventory />
              {/* Pages contributed by enabled plugins (contributions.ui.nav,
                  location "settings") — rendered here beside the roster they
                  belong to; absent when nothing declares one. */}
              <PluginNavList location="settings" />
            </ControlSection>

            <ControlSection
              id="mcp"
              label={ts("mcp")}
              description={t("mcpDescription")}
            >
              <McpInventory />
            </ControlSection>

            <ControlSection
              id="skills"
              label={ts("skills")}
              description={t("skillsDescription")}
            >
              <SkillsPage />
            </ControlSection>

            <ControlSection
              id="schedules"
              label={ts("schedules")}
              description={t("schedulesDescription")}
            >
              <SchedulesPage />
            </ControlSection>

            <ControlSection
              id="okf-graph"
              label={ts("okf-graph")}
              description={t("okfGraphDescription")}
            >
              <a
                href={assetUrl("/api/okf/graph")}
                target="_blank"
                rel="noopener noreferrer"
                className={buttonVariants({ size: "sm", variant: "secondary" })}
              >
                <ExternalLink className="size-4" aria-hidden />
                {t("openOkfGraph")}
              </a>
            </ControlSection>
          </div>
        </div>
      </div>
    </div>
  );
}
