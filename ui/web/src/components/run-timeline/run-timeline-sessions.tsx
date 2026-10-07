"use client";

// The sessions panel under the run timeline: the agent's sessions (the stretches between two
// compactions) with their understanding coverage and the estimated cost of describing the rest.
// Choosing sessions (checked, or a time range) and pressing Estimate plans the build without
// writing anything (`dry_run`); Build then queues exactly that and the panel polls its progress
// until the build ends, when the page refreshes the timeline's nodes. Collapsed by default so it
// takes no room from the chart.

import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { buttonVariants } from "@/components/ui/button";
import { errMsg } from "@/lib/contracts/errors";
import type { BuildProgressResponse, BuildRequest, BuildResponse, SessionOut } from "@/lib/contracts/types";
import { formatTokensCompact } from "@/lib/format/format-number";
import { formatShort } from "@/lib/format/time";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout/layout";
import { api } from "@/lib/transport/api";

import type { TimelineWindow } from "./timeline-model";

const POLL_MS = 3000;
const MIN_ZOOM_MS = 1000;

type Mode = "sessions" | "range";

/** The window to zoom the timeline to for a session; null when it has no placeable time. */
export function sessionWindow(session: Pick<SessionOut, "start" | "end">): TimelineWindow | null {
  if (session.start === null || session.end === null) return null;
  const start = Date.parse(session.start);
  const end = Date.parse(session.end);
  if (end - start >= MIN_ZOOM_MS) return { from: session.start, to: session.end };
  return { from: session.start, to: new Date(start + MIN_ZOOM_MS).toISOString() };
}

/** What the panel asks the build route for. */
function requestOf(mode: Mode, checked: readonly number[], from: string, to: string, dryRun: boolean): BuildRequest | null {
  if (mode === "sessions") {
    return checked.length === 0 ? null : { sessions: [...checked].sort((a, b) => a - b), dry_run: dryRun };
  }
  if (from === "" && to === "") return null;
  return {
    from: from === "" ? null : new Date(from).toISOString(),
    to: to === "" ? null : new Date(to).toISOString(),
    dry_run: dryRun,
  };
}

const ended = (phase: BuildProgressResponse["phase"]) => phase === "done" || phase === "failed";

export function RunTimelineSessions({
  agentId,
  onZoom,
  onBuildEnded,
}: {
  agentId: number;
  /** Zoom the timeline to a session's extent. */
  onZoom: (window: TimelineWindow, label: string) => void;
  /** A build reached its end: the timeline's nodes may have changed. */
  onBuildEnded: () => void;
}) {
  const t = useTranslations("runTimelineSessions");
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>("sessions");
  const [checked, setChecked] = useState<number[]>([]);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [estimate, setEstimate] = useState<{ key: string; result: BuildResponse } | null>(null);
  const [started, setStarted] = useState<BuildResponse | null>(null);

  const sessions = useQuery({
    queryKey: ["agent-sessions", agentId],
    queryFn: () => api.getAgentSessions(agentId),
    enabled: open,
  });
  const selectionKey = JSON.stringify(requestOf(mode, checked, from, to, true));
  const dryRun = useMutation({
    mutationFn: (body: BuildRequest) => api.postUnderstandingBuild(agentId, body),
    onSuccess: (result) => setEstimate({ key: selectionKey, result }),
  });
  const submit = useMutation({
    mutationFn: (body: BuildRequest) => api.postUnderstandingBuild(agentId, body),
    onSuccess: (result) => {
      setStarted(result);
      void sessions.refetch();
    },
  });
  const buildId = started?.build_id ?? null;
  const progress = useQuery({
    queryKey: ["understanding-build", agentId, buildId],
    queryFn: () => api.getUnderstandingBuild(agentId, buildId ?? 0),
    enabled: buildId !== null,
    refetchInterval: (query) => {
      const phase = query.state.data?.phase;
      return phase !== undefined && ended(phase) ? false : POLL_MS;
    },
  });

  // The timeline is refreshed once per build, when its progress first reads as ended.
  const refreshed = useRef<number | null>(null);
  const phase = progress.data?.phase;
  useEffect(() => {
    if (buildId !== null && phase !== undefined && ended(phase) && refreshed.current !== buildId) {
      refreshed.current = buildId;
      onBuildEnded();
      void sessions.refetch();
    }
  }, [buildId, phase, onBuildEnded, sessions]);

  const money = (value: number | null) => (value === null ? t("noPrice") : `$${value.toFixed(2)}`);
  const switchOff = sessions.data?.understanding_enabled === false;
  const estimateFresh = estimate !== null && estimate.key === selectionKey;
  const dryBody = requestOf(mode, checked, from, to, true);
  const buildBody = requestOf(mode, checked, from, to, false);
  const toggle = (number: number) =>
    setChecked((previous) => (previous.includes(number) ? previous.filter((n) => n !== number) : [...previous, number]));

  return (
    <section
      data-testid="run-timeline-sessions"
      aria-label={t("heading")}
      className="rounded-[10px] border border-border bg-card p-3 text-xs"
    >
      <button
        type="button"
        aria-expanded={open}
        data-testid="run-timeline-sessions-toggle"
        onClick={() => setOpen((value) => !value)}
        className={cn(FLEX, "w-full items-center justify-between text-sm font-semibold")}
      >
        <span>
          {t("heading")}
          {sessions.data ? ` (${sessions.data.sessions.length})` : ""}
        </span>
        <span className="text-xs font-normal text-muted-foreground">{open ? t("toggleClose") : t("toggleOpen")}</span>
      </button>
      {open ? (
        <div className="mt-2 space-y-2">
          {sessions.isPending ? <p className="text-muted-foreground">{t("loading")}</p> : null}
          {sessions.isError ? (
            <div role="alert" className="space-y-1 text-destructive">
              <p>{t("loadFailed")}</p>
              <button type="button" className={buttonVariants({ size: "sm" })} onClick={() => void sessions.refetch()}>
                {t("retry")}
              </button>
            </div>
          ) : null}
          {switchOff ? (
            <p data-testid="run-timeline-sessions-switch-off" className="rounded border border-border bg-muted/40 p-2">
              {t("switchOff")}
            </p>
          ) : null}
          {sessions.data?.sessions.length === 0 ? <p className="text-muted-foreground">{t("none")}</p> : null}
          {sessions.data && sessions.data.sessions.length > 0 ? (
            <>
              <p className="text-muted-foreground">{t("model", { model: sessions.data.model })}</p>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[34rem] border-collapse text-left tabular-nums">
                  <thead className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    <tr>
                      <th className="w-6 py-1">
                        <span className="sr-only">{t("colSelect")}</span>
                      </th>
                      <th>{t("colNumber")}</th>
                      <th>{t("colRange")}</th>
                      <th>{t("colMessages")}</th>
                      <th>{t("colPeak")}</th>
                      <th>{t("colCoverage")}</th>
                      <th>{t("colCost")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sessions.data.sessions.map((session) => {
                      const window = sessionWindow(session);
                      return (
                        <tr key={session.number} data-testid="run-timeline-session-row" className="border-t border-border">
                          <td className="py-1">
                            <input
                              type="checkbox"
                              aria-label={t("selectRow", { number: session.number })}
                              checked={checked.includes(session.number)}
                              disabled={mode !== "sessions"}
                              onChange={() => toggle(session.number)}
                            />
                          </td>
                          <td>
                            <button
                              type="button"
                              data-testid="run-timeline-session-zoom"
                              title={t("zoomTo", { number: session.number })}
                              aria-label={t("zoomTo", { number: session.number })}
                              disabled={window === null}
                              onClick={() => {
                                if (window !== null) onZoom(window, `${t("heading")} ${session.number}`);
                              }}
                              className="font-mono underline-offset-2 hover:underline disabled:no-underline"
                            >
                              #{session.number}
                              {session.boundary_checkpoint_id === null ? ` · ${t("inProgress")}` : ""}
                            </button>
                          </td>
                          <td>
                            {session.start && session.end ? `${formatShort(session.start)} – ${formatShort(session.end)}` : "—"}
                          </td>
                          <td>{session.messages}</td>
                          <td>{formatTokensCompact(session.peak_input_tokens)}</td>
                          <td data-testid="run-timeline-session-coverage">
                            {t("coverageValue", {
                              status: t(`coverage_${session.coverage.status}`),
                              percent: Math.round(session.coverage.ratio * 100),
                            })}
                          </td>
                          <td>{money(session.estimate.cost_usd)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              <div className={cn(FLEX, "flex-wrap items-center gap-3")}>
                {(["sessions", "range"] as const).map((value) => (
                  <label key={value} className={cn(FLEX, "items-center gap-1")}>
                    <input type="radio" name="sessions-mode" checked={mode === value} onChange={() => setMode(value)} />
                    {value === "sessions" ? t("modeSessions") : t("modeRange")}
                  </label>
                ))}
                {mode === "range" ? (
                  <>
                    <label className={cn(FLEX, "items-center gap-1")}>
                      {t("from")}
                      <input
                        type="datetime-local"
                        data-testid="run-timeline-sessions-from"
                        value={from}
                        onChange={(event) => setFrom(event.target.value)}
                        className="rounded border border-border bg-background px-1 py-0.5"
                      />
                    </label>
                    <label className={cn(FLEX, "items-center gap-1")}>
                      {t("to")}
                      <input
                        type="datetime-local"
                        data-testid="run-timeline-sessions-to"
                        value={to}
                        onChange={(event) => setTo(event.target.value)}
                        className="rounded border border-border bg-background px-1 py-0.5"
                      />
                    </label>
                  </>
                ) : null}
              </div>

              <div className={cn(FLEX, "items-center gap-2")}>
                <button
                  type="button"
                  data-testid="run-timeline-sessions-estimate"
                  disabled={dryBody === null || dryRun.isPending}
                  onClick={() => dryBody && dryRun.mutate(dryBody)}
                  className={buttonVariants({ size: "sm", variant: "outline" })}
                >
                  {dryRun.isPending ? t("estimating") : t("estimate")}
                </button>
                <button
                  type="button"
                  data-testid="run-timeline-sessions-build"
                  disabled={!estimateFresh || buildBody === null || submit.isPending}
                  title={estimateFresh ? undefined : t("buildNeedsEstimate")}
                  onClick={() => buildBody && submit.mutate(buildBody)}
                  className={buttonVariants({ size: "sm" })}
                >
                  {submit.isPending ? t("submitting") : t("build")}
                </button>
              </div>
              {dryRun.isError ? (
                <p role="alert" className="text-destructive">
                  {t("estimateFailed", { message: errMsg(dryRun.error) })}
                </p>
              ) : null}
              {submit.isError ? (
                <p role="alert" className="text-destructive">
                  {t("buildFailed", { message: errMsg(submit.error) })}
                </p>
              ) : null}
              {estimateFresh ? (
                <div data-testid="run-timeline-sessions-estimate-result" className="space-y-0.5 rounded border border-border p-2">
                  <p>
                    {t("estimateResult", {
                      jobs: estimate.result.estimate.jobs,
                      input: formatTokensCompact(estimate.result.estimate.input_tokens),
                      output: formatTokensCompact(estimate.result.estimate.output_tokens),
                      cost: money(estimate.result.estimate.cost_usd),
                    })}
                  </p>
                  {estimate.result.jobs.length === 0 ? <p className="text-muted-foreground">{t("estimateNothing")}</p> : null}
                  <p className="text-muted-foreground">{t("estimateBasis", { basis: estimate.result.cost_basis })}</p>
                </div>
              ) : null}
            </>
          ) : null}

          {started !== null && started.build_id !== null ? (
            <div data-testid="run-timeline-sessions-progress" className={cn("space-y-1 rounded border border-border p-2", MIN_W_0)}>
              <p className="font-semibold">{t("progressHeading", { id: started.build_id })}</p>
              <p>{t("buildStarted", { id: started.build_id, queued: started.queued, merged: started.merged })}</p>
              {!started.understanding_enabled ? (
                <p data-testid="run-timeline-sessions-queued-off" className="text-amber-600 dark:text-amber-400">
                  {t("queuedOff")}
                </p>
              ) : null}
              {progress.isError ? (
                <p role="alert" className="text-destructive">
                  {t("progressFailed")}
                </p>
              ) : null}
              {progress.data ? (
                <>
                  <p data-testid="run-timeline-sessions-phase">{t(`phase_${progress.data.phase}`)}</p>
                  <ul className="space-y-0.5">
                    {progress.data.jobs.map((job) => (
                      <li key={job.job_id} data-testid="run-timeline-sessions-job" className={cn(FLEX, "gap-2")}>
                        <span className={FLEX_1}>{t("job", { session: job.session, start: job.start_index, end: job.end_index })}</span>
                        <span className="tabular-nums">
                          {t("jobCost", { status: t(`jobStatus_${job.status}`), cost: money(job.cost_usd) })}
                        </span>
                      </li>
                    ))}
                  </ul>
                  <p>
                    {t("rebuild", {
                      status: progress.data.rebuild.status,
                      leaves: progress.data.rebuild.leaves,
                      cost: money(progress.data.rebuild.cost_usd),
                    })}
                  </p>
                  <p data-testid="run-timeline-sessions-total">{t("totalCost", { cost: money(progress.data.cost_usd) })}</p>
                </>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
