"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { useAlertClassActions, useAlertClasses, useAlertClassSamples } from "@/lib/transport/alert-classes";
import { errMsg } from "@/lib/contracts/errors";
import { FLEX, FLEX_1, FLEX_COL, MIN_W_0 } from "@/lib/layout/layout";
import { formatRelativeTime, type StatsWindowHours } from "@/lib/agents/sidebar";
import { formatAbsolute } from "@/lib/format/time";
import type { AlertClassRow } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";

const LEVEL_KEY = {
  warning: "alertLevelWarning",
  error: "alertLevelError",
  critical: "alertLevelCritical",
} as const;

const LEVEL_CLASS = {
  warning: "text-amber-600 dark:text-amber-400",
  error: "text-destructive",
  critical: "text-destructive",
} as const;

const rowKey = (row: AlertClassRow): string =>
  [row.level, row.event_name, row.source, row.process].join("\u0000");

/** The window's warning/error classes grouped like an error tracker: one row per class with its
 *  level, source, count and first/last occurrence; a row opens to its newest events and the
 *  dismiss / reopen action. Dismissed classes sit apart, collapsed. */
export function AlertClassList({ windowHours }: { windowHours: StatsWindowHours }) {
  const t = useTranslations("sidebar");
  const { data, error, isFetching, refetch } = useAlertClasses(windowHours, true);
  const [showDismissed, setShowDismissed] = useState(false);

  if (data === undefined) {
    return (
      <div className="px-3 pb-2 text-[11px] text-muted-foreground">
        {error ? (
          <span className="text-destructive">
            {t("alertLoadFailed")}: {errMsg(error)}{" "}
            <button type="button" className="underline" onClick={() => void refetch()}>
              {t("alertRetry")}
            </button>
          </span>
        ) : (
          t("alertLoading")
        )}
      </div>
    );
  }

  const active = data.classes.filter((row) => row.dismissal_id === null);
  const dismissed = data.classes.filter((row) => row.dismissal_id !== null);
  return (
    <div
      aria-busy={isFetching}
      className={cn("gap-1 border-t border-border px-3 py-2", FLEX, FLEX_COL)}
    >
      <span className="text-[10px] tracking-wide text-muted-foreground">
        {t("alertActiveHeading")}
      </span>
      {active.length === 0 ? (
        <span className="text-[11px] text-muted-foreground">{t("alertNoneActive")}</span>
      ) : (
        <ul className={cn("divide-y divide-border/60", FLEX, FLEX_COL)}>
          {active.map((row) => (
            <AlertClassItem key={rowKey(row)} row={row} windowHours={windowHours} />
          ))}
        </ul>
      )}
      {dismissed.length > 0 ? (
        <>
          <button
            type="button"
            aria-expanded={showDismissed}
            onClick={() => setShowDismissed((open) => !open)}
            className={cn(
              "mt-1 items-center gap-1 text-[10px] tracking-wide text-muted-foreground hover:text-foreground",
              FLEX,
            )}
          >
            {showDismissed ? (
              <ChevronDown className="size-3" aria-hidden />
            ) : (
              <ChevronRight className="size-3" aria-hidden />
            )}
            {t("alertDismissedHeading", { count: dismissed.length })}
          </button>
          {showDismissed ? (
            <ul className={cn("divide-y divide-border/60 opacity-70", FLEX, FLEX_COL)}>
              {dismissed.map((row) => (
                <AlertClassItem key={rowKey(row)} row={row} windowHours={windowHours} />
              ))}
            </ul>
          ) : null}
        </>
      ) : null}
      {data.total_classes > data.classes.length ? (
        <span className="text-[10px] text-muted-foreground">
          {t("alertTruncated", { shown: data.classes.length, total: data.total_classes })}
        </span>
      ) : null}
    </div>
  );
}

function AlertClassItem({
  row,
  windowHours,
}: {
  row: AlertClassRow;
  windowHours: StatsWindowHours;
}) {
  const t = useTranslations("sidebar");
  const [open, setOpen] = useState(false);
  const { dismiss, reopen } = useAlertClassActions();
  const isDismissed = row.dismissal_id !== null;
  const pending = dismiss.isPending || reopen.isPending;
  const failure = dismiss.error ?? reopen.error;
  const act = () => {
    if (row.dismissal_id !== null) reopen.mutate(row.dismissal_id);
    else dismiss.mutate(row);
  };
  return (
    <li className={cn("gap-1 py-1.5", MIN_W_0, FLEX, FLEX_COL)}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={cn("w-full gap-0.5 text-left", FLEX, FLEX_COL)}
      >
        <span className={cn("w-full items-baseline gap-1.5", FLEX)}>
          <span className={cn("shrink-0 font-mono text-[9px] font-medium", LEVEL_CLASS[row.level])}>
            {t(LEVEL_KEY[row.level])}
          </span>
          <span className={cn("truncate font-mono text-[11px]", FLEX_1, MIN_W_0)} title={row.event_name}>
            {row.event_name}
          </span>
          <span className="shrink-0 font-mono text-[11px] tabular-nums">{`×${row.count}`}</span>
        </span>
        <span className="truncate text-[10px] text-muted-foreground">
          {row.source}
          {row.process !== "" ? ` · ${row.process}` : ""}
        </span>
        <span className="text-[10px] text-muted-foreground">
          {t("alertFirstLast", {
            first: formatRelativeTime(row.first_seen),
            last: formatRelativeTime(row.last_seen),
          })}
        </span>
      </button>
      {open ? (
        <div className={cn("gap-1.5 pt-0.5", FLEX, FLEX_COL)}>
          <AlertClassSamples row={row} windowHours={windowHours} />
          <div className={cn("items-center gap-2", FLEX)}>
            <button
              type="button"
              disabled={pending}
              onClick={act}
              className="rounded border border-border px-2 py-0.5 text-[10px] hover:bg-sidebar-accent disabled:opacity-50"
            >
              {isDismissed ? t("alertReopen") : t("alertDismiss")}
            </button>
            {failure ? (
              <span className="truncate text-[10px] text-destructive" title={errMsg(failure)}>
                {t("alertActionFailed", { error: errMsg(failure) })}
              </span>
            ) : null}
          </div>
        </div>
      ) : null}
    </li>
  );
}

function AlertClassSamples({
  row,
  windowHours,
}: {
  row: AlertClassRow;
  windowHours: StatsWindowHours;
}) {
  const t = useTranslations("sidebar");
  const { data, error, isLoading } = useAlertClassSamples(row, windowHours, true);
  if (isLoading) return <span className="text-[10px] text-muted-foreground">{t("alertLoading")}</span>;
  if (error)
    return (
      <span className="text-[10px] text-destructive">
        {t("alertSamplesFailed")}: {errMsg(error)}
      </span>
    );
  if (data === undefined || data.length === 0)
    return <span className="text-[10px] text-muted-foreground">{t("alertNoSamples")}</span>;
  return (
    <div className={cn("gap-1", FLEX, FLEX_COL)}>
      <span className="text-[10px] tracking-wide text-muted-foreground">{t("alertSamples")}</span>
      {data.map((sample, index) => (
        <div key={`${sample.ts}-${index}`} className={cn("gap-0.5 rounded bg-sidebar-accent/40 px-1.5 py-1", FLEX, FLEX_COL)}>
          <span className="break-words font-mono text-[10px]">
            {sample.message ?? JSON.stringify(sample.attributes)}
          </span>
          <span className="break-words text-[10px] text-muted-foreground" title={formatAbsolute(sample.ts)}>
            {[
              formatRelativeTime(sample.ts),
              sample.machine,
              sample.agent_id !== null ? t("alertAgent", { id: sample.agent_id }) : null,
            ]
              .filter((part) => part !== null && part !== "")
              .join(" · ")}
          </span>
        </div>
      ))}
    </div>
  );
}
