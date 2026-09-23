import { RunTimelineChartSkeleton } from "@/components/run-timeline/run-timeline-skeleton";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

export default function Loading() {
  return (
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header aria-hidden="true" className="border-b border-border px-4 py-2">
        <div className="h-7 w-64 animate-pulse rounded bg-muted-foreground/10" />
      </header>
      <div className="overflow-y-auto">
        <div className="mx-auto max-w-6xl space-y-5 p-6">
          <div aria-hidden="true" className="ml-auto h-9 w-3/4 animate-pulse rounded bg-muted-foreground/10" />
          <div aria-hidden="true" className="h-[74px] animate-pulse rounded border border-border bg-card" style={{ marginTop: 0 }} />
          <RunTimelineChartSkeleton />
        </div>
      </div>
    </main>
  );
}
