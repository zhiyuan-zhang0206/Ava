"use client";

import { useTranslations } from "next-intl";

import type { TimelineWindowOverride } from "@/components/run-timeline/request-level";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

import { dateTimeInputValue, RUN_TIMELINE_ZOOM_HOURS, zoomPresetLabel } from "../../_run-timeline-shared";

/** Share the wrapping controls with the route skeleton, including the range slot. */
export function RunTimelineControls({
  window,
  flipLayers = false,
  axis = "time",
  axisDisabled = true,
  loading = false,
  onPreset,
  onZoom,
  onFlip,
  onAxis,
  onReset,
}: {
  window?: TimelineWindowOverride;
  flipLayers?: boolean;
  axis?: "time" | "context";
  axisDisabled?: boolean;
  loading?: boolean;
  onPreset?: (hours: number) => void;
  onZoom?: (factor: number) => void;
  onFlip?: () => void;
  onAxis?: (axis: "time" | "context") => void;
  onReset?: () => void;
}) {
  const t = useTranslations("runTimeline");
  const resetLabel = axis === "context" ? t("resetAxis") : t("resetWindow");
  return (
    <div className={cn(FLEX, "pointer-events-none sticky top-0 z-10 justify-end px-4")}>
      <div
        className={cn(FLEX, "pointer-events-auto max-w-full flex-wrap justify-end gap-1 rounded border border-border bg-card p-1 shadow-sm")}
        aria-label={t("zoom")}
      >
        <span
          aria-hidden={!window}
          className={cn("max-w-full truncate px-1 font-mono text-[10px] text-muted-foreground", !window && "invisible")}
        >
          {t("windowRange", {
            from: dateTimeInputValue(window?.from ?? new Date(0).toISOString()).replace("T", " "),
            to: dateTimeInputValue(window?.to ?? new Date(0).toISOString()).replace("T", " "),
          })}
        </span>
        {RUN_TIMELINE_ZOOM_HOURS.map((hours) => (
          <button
            key={hours}
            type="button"
            disabled={loading}
            onClick={() => onPreset?.(hours)}
            className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
          >
            {zoomPresetLabel(hours)}
          </button>
        ))}
        <button
          type="button"
          disabled={loading}
          aria-label={t("zoomOut")}
          onClick={() => onZoom?.(1.6)}
          className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
        >
          −
        </button>
        <button
          type="button"
          disabled={loading}
          aria-label={t("zoomIn")}
          onClick={() => onZoom?.(0.625)}
          className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
        >
          +
        </button>
        <button
          type="button"
          disabled={loading}
          aria-pressed={flipLayers}
          aria-label={t("flipLayers")}
          onClick={() => onFlip?.()}
          className={cn(
            "rounded border px-2 py-1 font-mono text-xs",
            flipLayers ? "border-primary bg-primary/10 text-primary" : "border-border hover:bg-muted",
          )}
        >
          {t("flipLayers")}
        </button>
        <span role="group" aria-label={t("axisGroup")} className={cn(FLEX, "items-center gap-0.5")}>
          <button
            type="button"
            disabled={loading}
            aria-pressed={axis === "time"}
            onClick={() => onAxis?.("time")}
            className={cn(
              "rounded border px-2 py-1 font-mono text-xs",
              axis === "time" ? "border-primary bg-primary/10 text-primary" : "border-border hover:bg-muted",
            )}
          >
            {t("axisTime")}
          </button>
          <button
            type="button"
            aria-pressed={axis === "context"}
            disabled={loading || axisDisabled}
            title={axisDisabled ? t("axisDisabled") : t("axisContextTitle")}
            onClick={() => onAxis?.("context")}
            className={cn(
              "rounded border px-2 py-1 font-mono text-xs",
              axis === "context" ? "border-primary bg-primary/10 text-primary" : "border-border hover:bg-muted",
              axisDisabled && "opacity-50",
            )}
          >
            {t("axisContext")}
          </button>
        </span>
        <button
          type="button"
          disabled={loading}
          aria-label={resetLabel}
          onClick={onReset}
          className="rounded border border-border px-2 py-1 font-mono text-xs hover:bg-muted"
        >
          {resetLabel}
        </button>
      </div>
    </div>
  );
}
