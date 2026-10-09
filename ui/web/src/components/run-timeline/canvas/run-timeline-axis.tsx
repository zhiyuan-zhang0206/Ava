"use client";

// The time axis under the rows: the zoom buttons and the round-number ticks of the viewport.

import { useTranslations } from "next-intl";

import { buttonVariants } from "@/components/ui/button";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

import { axisTicks, zoomView, type AxisMap, type Viewport } from "../model/timeline-model";

const BUTTON_ZOOM = 0.5;

export function RunTimelineAxis({
  view,
  base,
  onView,
  axis,
}: {
  view: Viewport;
  /** The whole loaded extent: "reset" returns to it. */
  base: Viewport;
  onView: (view: Viewport) => void;
  axis: AxisMap;
}) {
  const t = useTranslations("runTimeline");
  const ticks = axisTicks(view);
  const zoomButton = (factor: number) => onView(zoomView(axis, view, base, 0.5, factor));
  const atBase = view.from <= base.from && view.to >= base.to;
  return (
    <div className={cn(FLEX, "gap-2 text-[10px] text-muted-foreground")}>
        <div className={cn(FLEX, "w-20 shrink-0 justify-end gap-0.5")}>
          <button
            type="button"
            aria-label={t("zoomIn")}
            title={t("zoomIn")}
            onClick={() => zoomButton(BUTTON_ZOOM)}
            className={cn(buttonVariants({ size: "sm", variant: "ghost" }), "h-5 px-1.5 text-xs")}
          >
            +
          </button>
          <button
            type="button"
            aria-label={t("zoomOut")}
            title={t("zoomOut")}
            onClick={() => zoomButton(1 / BUTTON_ZOOM)}
            className={cn(buttonVariants({ size: "sm", variant: "ghost" }), "h-5 px-1.5 text-xs")}
          >
            −
          </button>
          <button
            type="button"
            aria-label={t("zoomReset")}
            title={t("zoomReset")}
            disabled={atBase}
            onClick={() => onView(base)}
            className={cn(buttonVariants({ size: "sm", variant: "ghost" }), "h-5 px-1.5 text-xs")}
          >
            ⤢
          </button>
        </div>
        <div className={cn("relative h-4 grow font-mono tabular-nums", MIN_W_0)} data-testid="run-timeline-axis">
          {ticks.map((tick) => (
            <span
              key={tick.left}
              className="absolute top-0 -translate-x-1/2 whitespace-nowrap"
              style={{ left: `${tick.left}%` }}
            >
              {tick.label}
            </span>
          ))}
        </div>
      </div>
  );
}
