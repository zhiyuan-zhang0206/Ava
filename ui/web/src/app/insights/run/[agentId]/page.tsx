"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { RunTimelineChart, type RunTimelineChartLabels } from "@/components/run-timeline/run-timeline-chart";
import {
  bucketLabel,
  centerZoomWindow,
  needsBuckets,
  pickBucketSeconds,
  usesTimelineBuckets,
  type TimelineWindowOverride,
} from "@/components/run-timeline/request-level";
import { buttonVariants } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatTokensCompact } from "@/lib/item-summary";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout";
import type { RunTimelineResponse } from "@/lib/types";
import { useUserSettings } from "@/lib/use-user-settings";
import { cn } from "@/lib/utils";

const ZOOM_WINDOWS = [24, 12, 6, 1, 0.5] as const;
const RUN_TIMELINE_WINDOW_HOURS_SETTING = "display.run_timeline_window_hours";
const RUN_TIMELINE_WINDOW_HOURS_DEFAULT = 2;

function runTimelineWindowHours(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : RUN_TIMELINE_WINDOW_HOURS_DEFAULT;
}

function dateTimeInputValue(iso: string): string {
  const date = new Date(iso);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function chartLabels(t: ReturnType<typeof useTranslations<"runTimeline">>): RunTimelineChartLabels {
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
    moreEvents: (count, summary) => t("moreEvents", { count, summary }),
    turnDetails: t("turnDetails"),
    timeRange: t("timeRange"),
    activeSeconds: t("activeSeconds"),
    latency: t("latency"),
    executions: t("executions"),
    tool: t("tool"),
    duration: t("duration"),
    status: t("status"),
    succeeded: t("succeeded"),
    failed: t("failed"),
    anomalies: t("anomalies"),
    none: t("none"),
    noExecutions: t("noExecutions"),
    closeDetails: t("closeDetails"),
    eventDetails: t("eventDetails"),
    kind: t("kind"),
    timestamp: t("timestamp"),
    detail: t("detail"),
  };
}

/** Run-level tracing page. The backend selects the initialized-context session on first load. */
export default function RunTimelinePage({
  params,
}: {
  params: Promise<{ agentId: string }>;
}) {
  const t = useTranslations("runTimeline");
  const [agentId, setAgentId] = useState<number | null>(null);
  const [paramsResolved, setParamsResolved] = useState(false);
  const [session, setSession] = useState<"compact" | "current">("compact");
  const { settings, isLoading: settingsLoading } = useUserSettings();
  const configuredWindowHours = runTimelineWindowHours(
    settings[RUN_TIMELINE_WINDOW_HOURS_SETTING],
  );
  const initialWindowOverride = useMemo<TimelineWindowOverride | null>(() => {
    if (settingsLoading || typeof window === "undefined") return null;
    const now = new Date();
    return {
      from: new Date(now.getTime() - configuredWindowHours * 60 * 60 * 1000).toISOString(),
      to: now.toISOString(),
    };
  }, [configuredWindowHours, settingsLoading]);
  // `undefined` means settings have not supplied the one-time initial window;
  // `null` remains the user's explicit reset to the full session.
  const [selectedWindowOverride, setWindowOverride] = useState<
    TimelineWindowOverride | null | undefined
  >(undefined);
  const windowOverride =
    selectedWindowOverride === undefined ? initialWindowOverride : selectedWindowOverride;
  const windowReady = selectedWindowOverride !== undefined || initialWindowOverride !== null;
  const [fromInput, setFromInput] = useState("");
  const [toInput, setToInput] = useState("");

  useEffect(() => {
    let cancelled = false;
    params
      .then(({ agentId: value }) => {
        const parsed = Number(value);
        if (!cancelled) {
          setAgentId(Number.isFinite(parsed) && parsed >= 0 ? parsed : null);
          setParamsResolved(true);
        }
      })
      .catch(() => {
        if (!cancelled) setParamsResolved(true);
      });
    return () => {
      cancelled = true;
    };
  }, [params]);

  const safeAgentId = agentId ?? 0;
  const requestsBucketsUpfront = usesTimelineBuckets(windowOverride);
  const turnQuery = useQuery({
    queryKey: ["run-timeline", agentId, windowOverride?.from ?? null, windowOverride?.to ?? null, session, "turn"],
    queryFn: () => api.getRunTimeline(safeAgentId, { ...(windowOverride ?? {}), session }),
    enabled: agentId !== null && windowReady && !requestsBucketsUpfront,
    placeholderData: keepPreviousData,
  });
  const densityWindow = requestsBucketsUpfront ? windowOverride : turnQuery.data?.window;
  const spanMs = densityWindow
    ? Date.parse(densityWindow.to) - Date.parse(densityWindow.from)
    : 60_000;
  const turnCount = requestsBucketsUpfront ? null : (turnQuery.data?.meta.n_turns ?? null);
  const bucketSeconds = pickBucketSeconds(spanMs, turnCount);
  const shouldBucket = requestsBucketsUpfront || needsBuckets(turnCount ?? 0);
  const bucketQuery = useQuery({
    queryKey: [
      "run-timeline",
      agentId,
      windowOverride?.from ?? null,
      windowOverride?.to ?? null,
      session,
      "bucket",
      bucketLabel(bucketSeconds),
    ],
    queryFn: () =>
      api.getRunTimeline(safeAgentId, {
        ...(windowOverride ?? {}),
        level: "bucket",
        bucket: `${bucketSeconds}s`,
        session,
      }),
    enabled: agentId !== null && windowReady && shouldBucket,
    placeholderData: keepPreviousData,
  });
  const timeline = shouldBucket ? bucketQuery.data : turnQuery.data;
  const timelinePending = shouldBucket ? bucketQuery.isPending : turnQuery.isPending;
  const defaultFromInput = timeline ? dateTimeInputValue(timeline.window.from) : "";
  const defaultToInput = timeline ? dateTimeInputValue(timeline.window.to) : "";
  const selectedFromInput = fromInput || defaultFromInput;
  const selectedToInput = toInput || defaultToInput;

  const selectWindow = (next: TimelineWindowOverride) => {
    setWindowOverride(next);
    setFromInput(dateTimeInputValue(next.from));
    setToInput(dateTimeInputValue(next.to));
  };

  const setZoomWindow = (hours: number) => {
    const now = new Date();
    const presetSpanMs = hours * 60 * 60 * 1000;
    const next = timeline
      ? centerZoomWindow(
          timeline.window,
          presetSpanMs / (Date.parse(timeline.window.to) - Date.parse(timeline.window.from)),
          now,
        )
      : {
          from: new Date(now.getTime() - presetSpanMs).toISOString(),
          to: now.toISOString(),
        };
    selectWindow(next);
  };

  const zoomBy = (factor: number) => {
    const visibleWindow = timeline?.window ?? windowOverride;
    if (visibleWindow === null) return;
    selectWindow(centerZoomWindow(visibleWindow, factor, new Date()));
  };

  const resetWindow = () => {
    setWindowOverride(null);
    setFromInput("");
    setToInput("");
  };

  const drillBucket = (row: RunTimelineResponse["rows"][number]) => {
    if (!timeline || row.turn !== null) {
      throw new Error("Bucket drill-down requires an aggregated timeline row");
    }
    const drillTo = Math.min(
      Date.parse(row.start) + bucketSeconds * 1000,
      Date.parse(timeline.window.to),
    );
    selectWindow({ from: row.start, to: new Date(drillTo).toISOString() });
  };

  const applyWindow = () => {
    const from = new Date(selectedFromInput);
    const to = new Date(selectedToInput);
    if (Number.isNaN(from.getTime()) || Number.isNaN(to.getTime()) || from >= to) return;
    setWindowOverride({ from: from.toISOString(), to: to.toISOString() });
  };

  const selectSession = (nextSession: "compact" | "current") => {
    setSession(nextSession);
    resetWindow();
  };

  if (paramsResolved && agentId === null) {
    return (
      <main id="main-content">
        <p className="p-6 font-sans text-sm text-destructive">{t("invalidAgent")}</p>
      </main>
    );
  }

  return (
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header className={cn("items-center gap-3 border-b border-border px-4 py-2", FLEX)}>
        <Link href="/insights" className={buttonVariants({ size: "sm", variant: "ghost" })}>
          {t("backToInsights")}
        </Link>
        <div className={cn(FLEX_1, MIN_W_0)}>
          <h1 className="truncate text-sm font-semibold">{t("title", { agentId: agentId ?? "—" })}</h1>
        </div>
      </header>

      <div className="overflow-y-auto">
        <div className="mx-auto max-w-6xl space-y-5 p-6">
          <div className={cn(FLEX, "pointer-events-none sticky top-0 z-10 justify-end px-4")}>
            <div
              className={cn(FLEX, "pointer-events-auto max-w-full flex-wrap justify-end gap-1 rounded border border-border bg-card p-1 shadow-sm")}
              aria-label={t("zoom")}
            >
              {timeline ? (
                <span className="max-w-full truncate px-1 font-mono text-2xs text-muted-foreground">
                  {t("windowRange", {
                    from: dateTimeInputValue(timeline.window.from).replace("T", " "),
                    to: dateTimeInputValue(timeline.window.to).replace("T", " "),
                  })}
                </span>
              ) : null}
              {ZOOM_WINDOWS.map((hours) => (
                <button
                  key={hours}
                  type="button"
                  onClick={() => setZoomWindow(hours)}
                  className="rounded border border-border px-2 py-1 font-sans text-xs hover:bg-muted"
                >
                  {hours >= 1 ? `${hours}h` : "30m"}
                </button>
              ))}
              <button
                type="button"
                aria-label={t("zoomOut")}
                onClick={() => zoomBy(2)}
                className="rounded border border-border px-2 py-1 font-sans text-xs hover:bg-muted"
              >
                −
              </button>
              <button
                type="button"
                aria-label={t("zoomIn")}
                onClick={() => zoomBy(0.5)}
                className="rounded border border-border px-2 py-1 font-sans text-xs hover:bg-muted"
              >
                +
              </button>
              <button
                type="button"
                aria-label={t("resetWindow")}
                onClick={resetWindow}
                className="rounded border border-border px-2 py-1 font-sans text-xs hover:bg-muted"
              >
                {t("resetWindow")}
              </button>
            </div>
          </div>

          <section
            className="space-y-3 rounded border border-border bg-card p-4"
            style={{ marginTop: 0 }}
          >
            <div>
              <h2 className="text-sm font-semibold">{t("session")}</h2>
              <p className="text-xs text-muted-foreground">
                {session === "compact" ? t("compactDescription") : t("currentDescription")}
              </p>
              <div className={cn(FLEX, "mt-2 gap-1")} role="group" aria-label={t("session")}>
                <button
                  type="button"
                  aria-pressed={session === "compact"}
                  onClick={() => selectSession("compact")}
                  className={cn(
                    "rounded border px-2 py-1 font-sans text-xs",
                    session === "compact" ? "border-primary bg-primary/10 text-primary" : "border-border hover:bg-muted",
                  )}
                >
                  {t("compactSession")}
                </button>
                <button
                  type="button"
                  aria-pressed={session === "current"}
                  onClick={() => selectSession("current")}
                  className={cn(
                    "rounded border px-2 py-1 font-sans text-xs",
                    session === "current" ? "border-primary bg-primary/10 text-primary" : "border-border hover:bg-muted",
                  )}
                >
                  {t("currentSession")}
                </button>
              </div>
            </div>
            <form
              className={cn(FLEX, "flex-wrap items-end gap-2")}
              onSubmit={(event) => {
                event.preventDefault();
                applyWindow();
              }}
            >
              <label className="grid gap-1 text-xs text-muted-foreground">
                {t("start")}
                <input
                  aria-label={t("start")}
                  type="datetime-local"
                  value={selectedFromInput}
                  onChange={(event) => setFromInput(event.target.value)}
                  className="rounded border border-border bg-background px-2 py-1 font-mono text-xs text-foreground"
                />
              </label>
              <label className="grid gap-1 text-xs text-muted-foreground">
                {t("end")}
                <input
                  aria-label={t("end")}
                  type="datetime-local"
                  value={selectedToInput}
                  onChange={(event) => setToInput(event.target.value)}
                  className="rounded border border-border bg-background px-2 py-1 font-mono text-xs text-foreground"
                />
              </label>
              <button type="submit" className={buttonVariants({ size: "sm" })}>
                {t("apply")}
              </button>
            </form>
            {timeline && timeline.meta.unmatched_turns + timeline.meta.fallback_turns > 0 ? (
              <p className="rounded border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200" role="alert">
                {t("unmatchedWarning", {
                  count: timeline.meta.unmatched_turns + timeline.meta.fallback_turns,
                })}
              </p>
            ) : null}
            {session === "compact" && timeline?.boundaries.has_activity_after_window ? (
              <p className="text-xs text-muted-foreground">
                {t("stillActiveAfterCompact", { count: timeline.boundaries.post_window_turns })}{" "}
                <button type="button" className="text-primary underline underline-offset-2" onClick={() => selectSession("current")}>
                  {t("viewCurrentSession")}
                </button>
              </p>
            ) : null}
          </section>

          {timeline ? (
            <>
              <section className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
                {[
                  [t("turns"), String(timeline.meta.n_turns)],
                  [t("active"), `${timeline.meta.active_s.toFixed(0)}s`],
                  [t("tokens"), `${formatTokensCompact(timeline.meta.tokens_in)} / ${formatTokensCompact(timeline.meta.tokens_out)}`],
                  [t("cost"), `$${timeline.meta.cost_usd.toFixed(2)}`],
                  [t("failures"), String(timeline.meta.n_exec_failed)],
                  [t("compacts"), String(timeline.meta.n_compact)],
                  [t("restarts"), String(timeline.meta.n_restart)],
                ].map(([label, value]) => (
                  <div key={label} className="rounded border border-border bg-card px-3 py-2">
                    <div className="text-2xs uppercase tracking-wide text-muted-foreground">{label}</div>
                    <div className="font-mono text-sm tabular-nums">{value}</div>
                  </div>
                ))}
              </section>
              <RunTimelineChart
                timeline={timeline}
                labels={chartLabels(t)}
                onDrillBucket={drillBucket}
                onZoomWindow={selectWindow}
              />
            </>
          ) : timelinePending ? (
            <p className="font-sans text-sm text-muted-foreground">{t("loading")}</p>
          ) : (
            <div className="space-y-2 font-sans text-sm text-destructive" role="alert">
              <p>{t("loadFailed")}</p>
              <button
                type="button"
                className={buttonVariants({ size: "sm" })}
                onClick={() => void (shouldBucket ? bucketQuery : turnQuery).refetch()}
              >
                {t("retry")}
              </button>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
