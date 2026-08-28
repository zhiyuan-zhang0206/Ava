"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

import { RunTimelineChart, type RunTimelineChartLabels } from "@/components/run-timeline/run-timeline-chart";
import { buttonVariants } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatTokensCompact } from "@/lib/item-summary";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

interface WindowOverride {
  from: string;
  to: string;
}

const ZOOM_WINDOWS = [24, 12, 6, 1, 0.5] as const;

function dateTimeInputValue(iso: string): string {
  const date = new Date(iso);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function chartLabels(t: ReturnType<typeof useTranslations<"runTimeline">>): RunTimelineChartLabels {
  return {
    time: t("time"),
    tokens: t("tokens"),
    eventRail: t("eventRail"),
    input: t("input"),
    output: t("output"),
    idle: t("idle"),
    turn: t("turn"),
    bucket: t("bucket"),
    cost: t("cost"),
    model: t("model"),
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
  const [windowOverride, setWindowOverride] = useState<WindowOverride | null>(null);
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
  const turnQuery = useQuery({
    queryKey: ["run-timeline", agentId, windowOverride?.from ?? null, windowOverride?.to ?? null, "turn"],
    queryFn: () => api.getRunTimeline(safeAgentId, windowOverride ?? undefined),
    enabled: agentId !== null,
    placeholderData: keepPreviousData,
  });
  const shouldBucket = (turnQuery.data?.meta.n_turns ?? 0) > 400;
  const bucket = shouldBucket ? "1h" : undefined;
  const bucketQuery = useQuery({
    queryKey: [
      "run-timeline",
      agentId,
      windowOverride?.from ?? null,
      windowOverride?.to ?? null,
      shouldBucket ? "bucket" : "turn",
      bucket ?? null,
    ],
    queryFn: () =>
      api.getRunTimeline(safeAgentId, {
        ...(windowOverride ?? {}),
        level: "bucket",
        bucket: "1h",
      }),
    enabled: agentId !== null && shouldBucket,
    placeholderData: keepPreviousData,
  });
  const timeline = shouldBucket ? bucketQuery.data ?? turnQuery.data : turnQuery.data;
  const defaultFromInput = timeline ? dateTimeInputValue(timeline.window.from) : "";
  const defaultToInput = timeline ? dateTimeInputValue(timeline.window.to) : "";
  const selectedFromInput = fromInput || defaultFromInput;
  const selectedToInput = toInput || defaultToInput;

  const setZoomWindow = (hours: number) => {
    const to = new Date();
    const from = new Date(to.getTime() - hours * 60 * 60 * 1000);
    const next = { from: from.toISOString(), to: to.toISOString() };
    setWindowOverride(next);
    setFromInput(dateTimeInputValue(next.from));
    setToInput(dateTimeInputValue(next.to));
  };

  const applyWindow = () => {
    const from = new Date(selectedFromInput);
    const to = new Date(selectedToInput);
    if (Number.isNaN(from.getTime()) || Number.isNaN(to.getTime()) || from >= to) return;
    setWindowOverride({ from: from.toISOString(), to: to.toISOString() });
  };

  if (paramsResolved && agentId === null) {
    return <p className="p-6 font-mono text-sm text-destructive">{t("invalidAgent")}</p>;
  }

  return (
    <main className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
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
          <section className="space-y-3 rounded border border-border bg-card p-4">
            <div className={cn(FLEX, "flex-wrap items-center justify-between gap-3")}>
              <div>
                <h2 className="text-sm font-semibold">{t("session")}</h2>
                <p className="text-xs text-muted-foreground">{t("description")}</p>
              </div>
              <div className={cn(FLEX, "flex-wrap gap-1")} aria-label={t("zoom")}>
                {ZOOM_WINDOWS.map((hours) => (
                  <button
                    key={hours}
                    type="button"
                    onClick={() => setZoomWindow(hours)}
                    className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
                  >
                    {hours >= 1 ? `${hours}h` : "30m"}
                  </button>
                ))}
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
              <RunTimelineChart timeline={timeline} labels={chartLabels(t)} />
            </>
          ) : turnQuery.isPending || bucketQuery.isPending ? (
            <p className="font-mono text-sm text-muted-foreground">{t("loading")}</p>
          ) : (
            <div className="space-y-2 font-mono text-sm text-destructive" role="alert">
              <p>{t("loadFailed")}</p>
              <button type="button" className={buttonVariants({ size: "sm" })} onClick={() => void turnQuery.refetch()}>
                {t("retry")}
              </button>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
