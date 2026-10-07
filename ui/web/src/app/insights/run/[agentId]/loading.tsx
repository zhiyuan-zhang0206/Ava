import { RunTimelineChartSkeleton } from "@/components/run-timeline/run-timeline-skeleton";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

export default function Loading() {
  return (
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header aria-hidden="true" className="border-b border-border px-4 py-2">
        <div className="h-7 w-64 animate-pulse rounded bg-muted-foreground/10" />
      </header>
      <div className="p-6">
        <RunTimelineChartSkeleton />
      </div>
    </main>
  );
}
