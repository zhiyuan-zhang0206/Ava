"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { ContextBreakdownCard } from "@/components/context-breakdown";
import {
  clampContextView,
  zoomContextViewAround,
  type TimelineContextView,
} from "@/components/run-timeline/context-view";
import { RunTimelineChart } from "@/components/run-timeline/run-timeline-chart";
import type { TimelineCrumbEntry } from "@/components/run-timeline/run-timeline-crumbs";
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
import { compareHref } from "@/lib/compare-links";
import { formatTokensCompact } from "@/lib/format-number";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout";
import type { RunTimelineResponse } from "@/lib/types";
import { useUserSettings } from "@/lib/use-user-settings";
import { cn } from "@/lib/utils";

import {
  RUN_TIMELINE_SUMMARY_VISIBLE_SETTING,
  RUN_TIMELINE_WINDOW_HOURS_SETTING,
  RUN_TIMELINE_ZOOM_HOURS,
  chartLabels,
  dateTimeInputValue,
  initialTimelineWindow,
  runTimelineWindowHours,
  zoomPresetLabel,
} from "../../_run-timeline-shared";

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
  const showTimelineSummaries = settings[RUN_TIMELINE_SUMMARY_VISIBLE_SETTING] !== false;
  const initialWindowOverride = useMemo<TimelineWindowOverride | null>(() => {
    if (settingsLoading || typeof window === "undefined") return null;
    return initialTimelineWindow(configuredWindowHours, new Date());
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
  const [flipLayers, setFlipLayers] = useState(false);
  const [trail, setTrail] = useState<TimelineCrumbEntry[]>([]);
  // P4-2b (#4023): the x projection and, on the context axis, the local char
  // viewport the gestures drive (pure display state — never a request key).
  const [axis, setAxis] = useState<"time" | "context">("time");
  const [contextView, setContextView] = useState<TimelineContextView | null>(null);

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

  // P4-1 (#4023, 3187 review condition): the focus trail is scoped to one
  // identity window — switching the session, or the route's agentId resolving
  // to a different agent in place, invalidates every crumb. The functional
  // updater returns the previous array when it is already empty, so a switch
  // without a trail costs no render.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- identity-keyed reset, not a render loop
    setTrail((previous) => (previous.length === 0 ? previous : []));
  }, [agentId, session]);

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
  const contextTotal = useMemo(() => {
    let total = 0;
    for (const message of timeline?.messages ?? []) {
      total += Math.max(0, message.chars);
    }
    return total;
  }, [timeline?.messages]);
  const axisDisabled = contextTotal === 0;
  const effectiveContextView = useMemo(
    () => contextView ?? { from: 0, to: contextTotal },
    [contextView, contextTotal],
  );

  const selectWindow = (next: TimelineWindowOverride) => {
    setWindowOverride(next);
    setFromInput(dateTimeInputValue(next.from));
    setToInput(dateTimeInputValue(next.to));
  };

  const setZoomWindow = (hours: number) => {
    setTrail([]);
    const now = new Date();
    const presetSpanMs = hours * 60 * 60 * 1000;
    const next = timeline
      ? centerZoomWindow(
          timeline.window,
          presetSpanMs / (Date.parse(timeline.window.to) - Date.parse(timeline.window.from)),
          now,
        )
      : initialTimelineWindow(hours, now);
    selectWindow(next);
  };

  const zoomBy = (factor: number) => {
    if (axis === "context") {
      setContextView((view) =>
        zoomContextViewAround(view ?? { from: 0, to: contextTotal }, factor, 0.5, contextTotal),
      );
      return;
    }
    const visibleWindow = timeline?.window ?? windowOverride;
    if (visibleWindow === null) return;
    selectWindow(centerZoomWindow(visibleWindow, factor, new Date()));
  };

  const resetWindow = () => {
    setWindowOverride(null);
    setFromInput("");
    setToInput("");
    setTrail([]);
  };

  /** P4-2b (#4023): "back to the full axis" — resets the local viewport on
   *  the context axis; on the time axis it resets the window (the fetch
   *  window IS the time axis' full extent). */
  const resetToFullAxis = () => {
    if (axis === "context") {
      setContextView({ from: 0, to: contextTotal });
      setTrail([]);
      return;
    }
    resetWindow();
  };

  /** P4-3 (#4023): the reset control's label is mode-aware — on the context
   *  axis it returns to the full axis extent (there is no window), matching
   *  the demo's full-axis wording; on the time axis it stays "Reset window". */
  const resetLabel = axis === "context" ? t("resetAxis") : t("resetWindow");

  /** P4-1 (#4023): double-click focus — pushes a crumb, then moves the window. */
  const focusWindow = (next: TimelineWindowOverride, label: string) => {
    setTrail((previous) => [...previous, { kind: "time", label, from: next.from, to: next.to }]);
    selectWindow(next);
  };

  /** P4-2b (#4023): switching the x projection resets the view to the full
   *  axis and clears the trail (the demo's setAxis semantics; crumbs never
   *  mix domains). */
  const selectAxis = (next: "time" | "context") => {
    if (next === axis) return;
    setAxis(next);
    setTrail([]);
    if (next === "context") {
      setContextView({ from: 0, to: contextTotal });
    }
  };

  /** P4-2b: viewport updates from the chart's gestures — clamped at this one
   *  choke point. */
  const updateContextView = (next: TimelineContextView) => {
    setContextView(clampContextView(next.from, next.to, contextTotal));
  };

  /** P4-2b: context-axis focus — pushes a char-range crumb, then moves the
   *  viewport (the mirror of focusWindow). */
  const focusContextView = (next: TimelineContextView, label: string) => {
    const clamped = clampContextView(next.from, next.to, contextTotal);
    setTrail((previous) => [
      ...previous,
      { kind: "context", label, from: clamped.from, to: clamped.to },
    ]);
    setContextView(clamped);
  };

  /** P4-1: a crumb restores its range; the root crumb returns to the initial window. */
  const selectCrumb = (index: number) => {
    if (index < 0) {
      resetToFullAxis();
      return;
    }
    const entry = trail.at(index);
    if (!entry) return;
    setTrail((previous) => previous.slice(0, index + 1));
    if (entry.kind === "context") {
      setContextView(clampContextView(entry.from, entry.to, contextTotal));
    } else {
      selectWindow({ from: entry.from, to: entry.to });
    }
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
    setTrail([]);
    setWindowOverride({ from: from.toISOString(), to: to.toISOString() });
  };

  const selectSession = (nextSession: "compact" | "current") => {
    setSession(nextSession);
    resetWindow();
  };

  // P4-2b (#4023): the context viewport projects the fetched messages. A
  // same-window refresh only moves the domain's end (clamp); a window change
  // replaces the data, so the view resets to the full axis and char-ranged
  // crumbs (meaningless against the new data) clear.
  useEffect(() => {
    if (axis !== "context") return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- domain-keyed clamp, not a render loop
    setContextView((view) =>
      view === null ? view : clampContextView(view.from, view.to, contextTotal),
    );
  }, [axis, contextTotal]);

  const windowKey = timeline ? `${timeline.window.from}|${timeline.window.to}` : null;
  const previousWindowKeyRef = useRef<string | null>(null);
  useEffect(() => {
    const previous = previousWindowKeyRef.current;
    previousWindowKeyRef.current = windowKey;
    if (previous === null || previous === windowKey || axis !== "context") return;
    setTrail([]);
    setContextView({ from: 0, to: contextTotal });
  }, [axis, contextTotal, windowKey]);

  // P4-2b (#4023): the character axis projects the message list; when a
  // refresh drops it (a degraded read), fall back to the time axis instead
  // of parking the chart on a blank, disabled context view.
  useEffect(() => {
    if (axis !== "context" || !axisDisabled) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-keyed fallback, not a render loop
    setAxis("time");
    setTrail([]);
  }, [axis, axisDisabled]);

  if (paramsResolved && agentId === null) {
    return (
      <main id="main-content">
        <p className="p-6 font-mono text-sm text-destructive">{t("invalidAgent")}</p>
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
        {agentId !== null ? (
          <Link
            href={compareHref([agentId])}
            className={buttonVariants({ size: "sm", variant: "ghost" })}
          >
            {t("compareOpen")}
          </Link>
        ) : null}
      </header>

      <div className="overflow-y-auto">
        <div className="mx-auto max-w-6xl space-y-5 p-6">
          <div className={cn(FLEX, "pointer-events-none sticky top-0 z-10 justify-end px-4")}>
            <div
              className={cn(FLEX, "pointer-events-auto max-w-full flex-wrap justify-end gap-1 rounded border border-border bg-card p-1 shadow-sm")}
              aria-label={t("zoom")}
            >
              {timeline ? (
                <span className="max-w-full truncate px-1 font-mono text-[10px] text-muted-foreground">
                  {t("windowRange", {
                    from: dateTimeInputValue(timeline.window.from).replace("T", " "),
                    to: dateTimeInputValue(timeline.window.to).replace("T", " "),
                  })}
                </span>
              ) : null}
              {RUN_TIMELINE_ZOOM_HOURS.map((hours) => (
                <button
                  key={hours}
                  type="button"
                  onClick={() => setZoomWindow(hours)}
                  className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
                >
                  {zoomPresetLabel(hours)}
                </button>
              ))}
              <button
                type="button"
                aria-label={t("zoomOut")}
                onClick={() => zoomBy(1.6)}
                className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
              >
                −
              </button>
              <button
                type="button"
                aria-label={t("zoomIn")}
                onClick={() => zoomBy(0.625)}
                className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
              >
                +
              </button>
              <button
                type="button"
                aria-pressed={flipLayers}
                aria-label={t("flipLayers")}
                onClick={() => setFlipLayers((value) => !value)}
                className={cn(
                  "rounded border px-2 py-1 font-mono text-xs",
                  flipLayers ? "border-primary bg-primary/10 text-primary" : "border-border hover:bg-muted",
                )}
              >
                {t("flipLayers")}
              </button>
              <span role="group" aria-label={t("axisGroup")} className={cn(FLEX, "items-center gap-0.5")}>
                <button
                  type="button"
                  aria-pressed={axis === "time"}
                  onClick={() => selectAxis("time")}
                  className={cn(
                    "rounded border px-2 py-1 font-mono text-xs",
                    axis === "time" ? "border-primary bg-primary/10 text-primary" : "border-border hover:bg-muted",
                  )}
                >
                  {t("axisTime")}
                </button>
                <button
                  type="button"
                  aria-pressed={axis === "context"}
                  disabled={axisDisabled}
                  title={axisDisabled ? t("axisDisabled") : t("axisContextTitle")}
                  onClick={() => selectAxis("context")}
                  className={cn(
                    "rounded border px-2 py-1 font-mono text-xs",
                    axis === "context" ? "border-primary bg-primary/10 text-primary" : "border-border hover:bg-muted",
                    axisDisabled && "opacity-50",
                  )}
                >
                  {t("axisContext")}
                </button>
              </span>
              <button
                type="button"
                aria-label={resetLabel}
                onClick={resetToFullAxis}
                className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
              >
                {resetLabel}
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
                    "rounded border px-2 py-1 font-mono text-xs",
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
                    "rounded border px-2 py-1 font-mono text-xs",
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
                    <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
                    <div className="font-mono text-sm tabular-nums">{value}</div>
                  </div>
                ))}
              </section>
              {/* P4-3 (#4023): the context breakdown card — the composer
                  panel's body rendered inline above the chart (demo order). */}
              {agentId !== null ? <ContextBreakdownCard agentId={agentId} /> : null}
              <RunTimelineChart
                timeline={timeline}
                labels={chartLabels(t)}
                onDrillBucket={drillBucket}
                onZoomWindow={selectWindow}
                showSummaries={showTimelineSummaries}
                flipLayers={flipLayers}
                trail={trail}
                onCrumbSelect={selectCrumb}
                onFocusWindow={focusWindow}
                withReadout
                axis={axis}
                contextView={effectiveContextView}
                contextTotal={contextTotal}
                onContextView={updateContextView}
                onContextFocus={focusContextView}
              />
            </>
          ) : timelinePending ? (
            <p className="font-mono text-sm text-muted-foreground">{t("loading")}</p>
          ) : (
            <div className="space-y-2 font-mono text-sm text-destructive" role="alert">
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
