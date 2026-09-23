// Shared plumbing for the run-timeline pages (single view + compare): the
// display settings they read, the window/zoom presets, and the chart-label
// mapping every lane renders with. One source for both pages — the single
// view and the compare view must offer the same window semantics.

import type { useTranslations } from "next-intl";

import type { RunTimelineChartLabels } from "@/components/run-timeline/run-timeline-chart";
import type { TimelineWindowOverride } from "@/components/run-timeline/request-level";

export const RUN_TIMELINE_WINDOW_HOURS_SETTING = "display.run_timeline_window_hours";
export const RUN_TIMELINE_COMPARE_MESSAGES_MAX_SETTING = "display.run_timeline_compare_messages_max";
// KEEP (task #3696 exception inventory): fallback when the per-user setting is
// unset — the compare view's per-lane strip budget. The M4 density probe (P4-4)
// measured the wheel-pan p95 at 313 ms with the server's 600 ceiling across 4
// lanes and 123 ms at 200 (the registered bound is 150 ms), so compare reads
// ask for 200 per lane and the server clamps the request to its own ceiling.
export const RUN_TIMELINE_COMPARE_MESSAGES_MAX_DEFAULT = 200;
// KEEP (task #3696 exception inventory): fallback when the per-user setting is
// unset — the smallest window the zoom control offers (RUN_TIMELINE_ZOOM_HOURS
// bottoms out at 0.5h), so the page opens focused on the freshest slice.
export const RUN_TIMELINE_WINDOW_HOURS_DEFAULT = 0.5;
export const RUN_TIMELINE_SUMMARY_VISIBLE_SETTING = "display.run_timeline_summary_visible";

/** Zoom presets in hours; 0.5 renders as "30m". */
export const RUN_TIMELINE_ZOOM_HOURS = [24, 12, 6, 1, 0.5] as const;

export function zoomPresetLabel(hours: number): string {
  return hours >= 1 ? `${hours}h` : "30m";
}

export function runTimelineWindowHours(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : RUN_TIMELINE_WINDOW_HOURS_DEFAULT;
}

export function runTimelineCompareMessagesMax(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? value
    : RUN_TIMELINE_COMPARE_MESSAGES_MAX_DEFAULT;
}

/** The settings-derived initial window: the freshest configured slice. */
export function initialTimelineWindow(hours: number, now: Date): TimelineWindowOverride {
  return {
    from: new Date(now.getTime() - hours * 60 * 60 * 1000).toISOString(),
    to: now.toISOString(),
  };
}

/** A `datetime-local` input value in local time. */
export function dateTimeInputValue(iso: string): string {
  const date = new Date(iso);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** One mapping from the runTimeline message namespace to chart labels. */
export function chartLabels(
  t: ReturnType<typeof useTranslations<"runTimeline">>,
): RunTimelineChartLabels {
  return {
    chart: t("chartAriaLabel"),
    visualization: t("visualizationAriaLabel"),
    time: t("time"),
    eventRail: t("eventRail"),
    input: t("input"),
    output: t("output"),
    turn: t("turn"),
    bucket: t("bucket"),
    cost: t("cost"),
    model: t("model"),
    empty: t("empty"),
    readerEmpty: t("readerEmpty"),
    moreEvents: (count, summary) => t("moreEvents", { count, summary }),
    turnDetails: t("turnDetails"),
    timeRange: t("timeRange"),
    activeSeconds: t("activeSeconds"),
    latency: t("latency"),
    executions: t("executions"),
    tool: t("tool"),
    status: t("status"),
    succeeded: t("succeeded"),
    failed: t("failed"),
    anomalies: t("anomalies"),
    none: t("none"),
    noExecutions: t("noExecutions"),
    closeDetails: t("closeDetails"),
    eventDetails: t("eventDetails"),
    layerDetails: t("layerDetails"),
    layerSummary: t("layerSummary"),
    pendingLabel: t("pendingLabel"),
    pendingExplainer: t("pendingExplainer"),
    pendingAria: t("pendingAria"),
    showMore: t("showMore"),
    showLess: t("showLess"),
    kind: t("kind"),
    timestamp: t("timestamp"),
    detail: t("detail"),
    crumbRoot: t("crumbRoot"),
    readoutIdle: t("readoutIdle"),
    stripAria: t("stripAria"),
    stripTruncated: t("stripTruncated"),
    stripMessageAria: (idx, kind, time, chars) => t("stripMessageAria", { idx, kind, time, chars }),
    stripPartLabels: {
      think: t("stripPartThink"),
      text: t("stripPartText"),
      call: t("stripPartCall"),
      out: t("stripPartOut"),
      note: t("stripPartNote"),
      "ib-agent": t("stripPartIbAgent"),
      "ib-user": t("stripPartIbUser"),
      "ib-sys": t("stripPartIbSys"),
      compact: t("stripPartCompact"),
      prompt: t("stripPartPrompt"),
      other: t("stripPartOther"),
    },
    legendTitle: t("legendTitle"),
    legendHint: t("legendHint"),
    legendLabels: {
      think: t("legendThink"),
      text: t("legendText"),
      call: t("legendCall"),
      out: t("legendOut"),
      note: t("legendNote"),
      "ib-agent": t("legendIbAgent"),
      "ib-user": t("legendIbUser"),
      sys: t("legendSys"),
      prompt: t("legendPrompt"),
    },
    readoutMessage: (idx, kind, time, chars, leaf) =>
      t("readoutMessage", { idx, kind, time, chars, leaf: leaf ?? t("none") }),
    messageDetails: t("messageDetails"),
    messageCharsLabel: t("messageCharsLabel"),
    messageSource: t("messageSource"),
    messageLabel: (idx) => t("messageLabel", { idx }),
    messageChars: (chars) => t("messageChars", { chars }),
    messagePart: (kind, chars) => t("messagePart", { kind, chars }),
    messageChain: t("messageChain"),
    messageFocus: t("messageFocus"),
    messageShowSummary: t("messageShowSummary"),
    messageBody: t("messageBody"),
    messageLoading: t("messageLoading"),
    messageLoadFailed: t("messageLoadFailed"),
    messageExpandFull: t("messageExpandFull"),
    messagePartTruncated: t("messagePartTruncated"),
    messageNoText: t("messageNoText"),
    retry: t("retry"),
    railHidden: t("railHidden"),
    railHiddenPending: t("railHiddenPending"),
    readoutPosition: (position) => t("readoutPosition", { position }),
    charsUnit: t("charsUnit"),
    charTicks: t("charTicks"),
  };
}
