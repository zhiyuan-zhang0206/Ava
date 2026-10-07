"use client";

import { FLEX, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

/** One labelled row of the run timeline: its label on the left, the track on the right. */
export function RowShell({
  label,
  height,
  testId,
  children,
}: {
  label: string;
  height: string;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn(FLEX, "items-stretch gap-2")} data-testid={testId}>
      <div className="w-20 shrink-0 self-center truncate text-right text-[11px] text-muted-foreground">
        {label}
      </div>
      <div
        data-track=""
        className={cn("relative rounded bg-muted/40 [touch-action:pan-y]", MIN_W_0, OVERFLOW_HIDDEN, height, "grow")}
      >
        {children}
      </div>
    </div>
  );
}
