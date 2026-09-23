"use client";

import { useTranslations } from "next-intl";

export function RunTimelineChartSkeleton() {
  const t = useTranslations("runTimeline");
  return (
    <section role="status" aria-label={t("loading")} className="space-y-2 rounded-[10px] border border-border bg-card p-3">
      <span className="sr-only">{t("loading")}</span>
      <div aria-hidden="true" className="h-[131px] animate-pulse rounded bg-muted-foreground/10" />
      <div aria-hidden="true" className="min-h-[50vh] animate-pulse rounded bg-muted-foreground/10" />
    </section>
  );
}
