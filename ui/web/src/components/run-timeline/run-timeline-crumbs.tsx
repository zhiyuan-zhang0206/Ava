"use client";

// The drill path above the chart: the root is the agent's whole lifetime, each
// crumb the window one drill narrowed to. Choosing a crumb steps back to it.

import { useTranslations } from "next-intl";

import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

import type { Crumb } from "./timeline-model";

export function RunTimelineCrumbs({
  trail,
  onSelect,
}: {
  trail: readonly Crumb[];
  /** `-1` is the root (the whole lifetime); otherwise the crumb's index. */
  onSelect: (index: number) => void;
}) {
  const t = useTranslations("runTimeline");
  return (
    <nav
      aria-label={t("crumbsAria")}
      data-testid="run-timeline-crumbs"
      className={cn(FLEX, "flex-wrap items-center gap-x-1 text-xs text-muted-foreground")}
    >
      <button
        type="button"
        aria-current={trail.length === 0 ? "location" : undefined}
        onClick={() => onSelect(-1)}
        className={cn("rounded px-1 hover:bg-muted", trail.length === 0 && "font-medium text-foreground")}
      >
        {t("crumbRoot")}
      </button>
      {trail.map((crumb, index) => (
        <span key={`${index}-${crumb.from}`} className={cn(FLEX, "items-center gap-1")}>
          <span aria-hidden="true">›</span>
          <button
            type="button"
            aria-current={index === trail.length - 1 ? "location" : undefined}
            onClick={() => onSelect(index)}
            className={cn(
              "max-w-[16rem] truncate rounded px-1 hover:bg-muted",
              index === trail.length - 1 && "font-medium text-foreground",
            )}
          >
            {crumb.label}
          </button>
        </span>
      ))}
    </nav>
  );
}
