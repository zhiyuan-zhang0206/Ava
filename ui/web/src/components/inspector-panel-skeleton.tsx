"use client";

import { useTranslations } from "next-intl";

import { useBreakpoint } from "@/lib/breakpoint";
import {
  BAR_DIVIDER_CLASS,
  BAR_HEIGHT_CLASS,
  FLEX,
  FLEX_1,
  FLEX_COL,
  MIN_H_0,
  MIN_W_0,
} from "@/lib/layout";
import { cn } from "@/lib/utils";

export function SectionSkeleton({
  title,
  rows = 2,
}: {
  title: string;
  rows?: number;
}) {
  return (
    <section aria-label={`${title} loading`} className="space-y-1.5">
      <div className="h-3 w-24 animate-pulse rounded bg-muted-foreground/20" />
      <div className="grid grid-cols-2 gap-1">
        {Array.from({ length: rows }, (_, index) => (
          <div
            key={index}
            className="h-10 animate-pulse rounded bg-muted-foreground/10"
          />
        ))}
      </div>
    </section>
  );
}

export function LiveSectionsSkeleton() {
  const t = useTranslations("inspector");
  return (
    <>
      <SectionSkeleton title={t("sectionShells")} rows={1} />
      <SectionSkeleton title={t("sectionLiveness")} rows={3} />
      <SectionSkeleton title={t("sectionConfigOverlay")} rows={1} />
    </>
  );
}

export function WindowedSectionsSkeleton() {
  const t = useTranslations("inspector");
  return (
    <>
      <SectionSkeleton title={t("sectionCost")} rows={4} />
      <SectionSkeleton title={t("sectionActivity")} rows={4} />
    </>
  );
}

export function InspectorPanelSkeleton() {
  const t = useTranslations("inspector");
  const { isLarge } = useBreakpoint();

  const body = (
    <>
      <header
        className={cn(
          "relative items-center gap-2 px-4",
          BAR_DIVIDER_CLASS,
          BAR_HEIGHT_CLASS,
          FLEX,
        )}
      >
        <div className="size-5 shrink-0 -ml-1 rounded bg-muted-foreground/10" />
        <span
          className={cn(
            "truncate font-mono text-xs tracking-wide text-muted-foreground",
            MIN_W_0,
            FLEX_1,
          )}
        >
          {t("title")}
        </span>
        <div className="h-4 w-12 animate-pulse rounded bg-muted-foreground/10" />
      </header>

      <div className={cn("overflow-y-auto px-4 py-3 text-xs", MIN_H_0, FLEX_1)}>
        <div className="space-y-4">
          <LiveSectionsSkeleton />
          <WindowedSectionsSkeleton />
          <div className="h-3 w-28 animate-pulse rounded bg-muted-foreground/10" />
        </div>
      </div>
    </>
  );

  if (isLarge) {
    return (
      <aside
        data-testid="inspector-panel-skeleton"
        className={cn("h-full w-full bg-background", FLEX, FLEX_COL, MIN_H_0)}
      >
        {body}
      </aside>
    );
  }

  return (
    <div className={cn("fixed inset-0 z-50", FLEX)}>
      <div className="absolute inset-0 bg-black/40" aria-hidden="true" />
      <aside
        data-testid="inspector-panel-skeleton"
        className={cn("relative w-full bg-background", FLEX, FLEX_COL)}
      >
        {body}
      </aside>
    </div>
  );
}
