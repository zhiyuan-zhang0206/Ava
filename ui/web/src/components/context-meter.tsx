"use client";

import { useTranslations } from "next-intl";

import type { ContextMeterWidth } from "@/lib/types";
import { cn } from "@/lib/utils";
import { OVERFLOW_HIDDEN } from "@/lib/layout";

/** Compact token count: 940 -> "940", 26_300 -> "26.3k", 1_000_000 -> "1.00M". */
export function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

// Width classes for the gauge track, keyed by the `display.context_meter_width`
// setting. "compact" is the original fixed width (w-16); "wide" is 3x that —
// at tens of thousands of tokens the occupancy fill inside a 64px bar reads as
// a single pixel-wide dot, so the setting's default is one tier above compact.
export const CONTEXT_METER_WIDTH_CLASS: Record<ContextMeterWidth, string> = {
  compact: "w-16",
  comfortable: "w-32",
  wide: "w-48",
};

/** Narrows an opaque `user_settings` value (JSONB — could be stale, from an
 *  older client, or unset) to a valid ContextMeterWidth, falling back to the
 *  setting's default rather than trusting an unsound cast. */
export function resolveContextMeterWidth(value: unknown): ContextMeterWidth {
  return value === "compact" || value === "comfortable" || value === "wide" ? value : "comfortable";
}

export interface ContextMeterProps {
  /** Current context-window occupancy (the last LLM call's input_tokens). */
  contextTokens: number;
  /** Model context window ceiling; 0 = unknown (gauge + thresholds hidden). */
  maxContextTokens: number;
  /** Wind-down-reminder threshold (soft) — 0 when unknown. */
  softCompactTokens: number;
  /** Force-compact ceiling (hard) — 0 when unknown. */
  hardCompactTokens: number;
  /** Gauge track width class — one of CONTEXT_METER_WIDTH_CLASS. Defaults to
   *  "compact" (the original fixed size) for callers that don't read the
   *  display setting. */
  barWidthClassName?: string;
  className?: string;
}

/** The composer's context readout: a thin gauge whose fill turns amber past the
 * soft (wind-down) threshold and red past the hard (force-compact) ceiling, with
 * tick marks at both, plus a numeric summary. The soft/hard values are the
 * agent model's own — a fraction of its context window (see
 * `shared/lm/context_budget.py`), so a 200K-window model shows smaller marks
 * than a 1M-window one.
 *
 * The caller only mounts this once `contextTokens > 0` (an agent that has run at
 * least one LLM call). When `maxContextTokens` is 0 (a model with no known
 * window) the gauge and thresholds are omitted and only the raw count shows. */
export function ContextMeter({
  contextTokens,
  maxContextTokens,
  softCompactTokens,
  hardCompactTokens,
  barWidthClassName = CONTEXT_METER_WIDTH_CLASS.compact,
  className,
}: ContextMeterProps) {
  const t = useTranslations("contextMeter");
  const hasWindow = maxContextTokens > 0;
  const hasThresholds = hasWindow && hardCompactTokens > 0;

  // Position everything as a percentage of the full window; clamp so a value
  // over the ceiling (occupancy briefly past hard before compaction fires)
  // still renders inside the bar.
  const pct = (v: number) => Math.min(100, Math.max(0, (v / maxContextTokens) * 100));
  const fillPct = hasWindow ? pct(contextTokens) : 0;

  // Fill color communicates urgency: neutral below soft, amber in the
  // wind-down band, red once past the force-compact ceiling.
  const overHard = hasThresholds && contextTokens > hardCompactTokens;
  const overSoft = hasThresholds && contextTokens > softCompactTokens;
  const fillColor = overHard
    ? "bg-destructive"
    : overSoft
      ? "bg-amber-500"
      : "bg-muted-foreground/60";

  const label = hasThresholds
    ? t("contextWindDown", {
        used: formatTokens(contextTokens),
        max: formatTokens(maxContextTokens),
        soft: formatTokens(softCompactTokens),
        hard: formatTokens(hardCompactTokens),
      })
    : hasWindow
      ? t("context", {
          used: formatTokens(contextTokens),
          max: formatTokens(maxContextTokens),
        })
      : t("contextUsed", { used: formatTokens(contextTokens) });

  return (
    <span
      data-testid="context-meter"
      className={cn("inline-flex items-center gap-1.5", className)}
    >
      {hasWindow ? (
        <span
          role="img"
          aria-label={label}
          className={cn(
            "relative inline-block h-1.5 shrink-0 rounded-full bg-muted",
            barWidthClassName,
            OVERFLOW_HIDDEN
          )}
        >
          <span
            data-testid="context-meter-fill"
            className={cn("absolute inset-y-0 left-0 rounded-full", fillColor)}
            style={{ width: `${fillPct}%` }}
          />
          {hasThresholds ? (
            <>
              <span
                data-testid="context-meter-soft-mark"
                className="absolute inset-y-0 w-px bg-amber-500/80"
                style={{ left: `${pct(softCompactTokens)}%` }}
              />
              <span
                data-testid="context-meter-hard-mark"
                className="absolute inset-y-0 w-px bg-destructive/80"
                style={{ left: `${pct(hardCompactTokens)}%` }}
              />
            </>
          ) : null}
        </span>
      ) : null}
      <span className="truncate tabular-nums">
        Context: {formatTokens(contextTokens)}
        {hasWindow ? `/${formatTokens(maxContextTokens)}` : ""}
        {hasThresholds
          ? t("soft", { soft: formatTokens(softCompactTokens), hard: formatTokens(hardCompactTokens) })
          : " tokens"}
      </span>
    </span>
  );
}
