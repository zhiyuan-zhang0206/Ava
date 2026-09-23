"use client";

import { useTranslations } from "next-intl";
import type { ReactNode, Ref } from "react";

import { FLEX_1, MIN_H_0, MIN_W_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

/** The page owns the reader's viewport; the chart portals its local selection here. */
export function RunTimelineWorkspace({
  children,
  readerRef,
  pending = true,
}: {
  children: ReactNode;
  readerRef?: Ref<HTMLElement>;
  pending?: boolean;
}) {
  const t = useTranslations("runTimeline");
  return (
    <div className={cn("grid xl:grid-cols-[minmax(0,1fr)_440px]", MIN_H_0, FLEX_1)}>
      <div data-testid="run-timeline-main" className={cn("overflow-y-auto", MIN_W_0)}>
        <div className="mx-auto max-w-6xl space-y-5 p-6 xl:max-w-none">{children}</div>
      </div>
      <aside
        ref={readerRef}
        data-testid="run-timeline-reader"
        aria-label={t("readerTitle")}
        className={cn("hidden overflow-y-auto border-l border-border bg-card px-4 py-3.5 [overflow-wrap:anywhere] xl:block", MIN_H_0)}
      >
        {pending ? <p className="text-sm text-muted-foreground">{t("readerEmpty")}</p> : null}
      </aside>
    </div>
  );
}
