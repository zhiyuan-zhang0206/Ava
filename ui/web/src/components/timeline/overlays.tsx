"use client";

// Floating overlay chrome for the timeline view: the pull-to-load indicator,
// the cold-load spinner, and the scroll-to-bottom button.

import { ArrowDown, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";
import { FLEX } from "@/lib/layout";

export interface PullToLoadIndicatorProps {
  pullDistance: number;
  pullThreshold?: number;
  loadingOlder: boolean;
}

// Pull-down-to-load indicator — circular progress ring that fills up as the user
// pulls down at the top of the timeline viewport. When filled and released, it
// enters the loadingOlder state (spinning loader) while older compact history is
// fetched. Floating overlay (not in flow) to guarantee zero layout shift / jitter.
export function PullToLoadIndicator({
  pullDistance,
  pullThreshold = 56,
  loadingOlder,
}: PullToLoadIndicatorProps) {
  const t = useTranslations("timeline");
  const progress = Math.min(1, Math.max(0, pullDistance / pullThreshold));
  const isVisible = loadingOlder || pullDistance > 0;
  const isFilled = progress >= 1;

  const radius = 7;
  const circumference = 2 * Math.PI * radius; // ~43.98
  const strokeDashoffset = circumference * (1 - progress);

  return (
    <div
      role="status"
      aria-label={t("loadingEarlier")}
      aria-hidden={!isVisible}
      data-testid="pull-down-load-indicator"
      data-pull-progress={progress.toFixed(2)}
      data-loading={loadingOlder}
      data-filled={isFilled}
      style={{
        transform: `translate(-50%, ${loadingOlder ? 12 : Math.min(pullDistance * 0.4, 20)}px) scale(${loadingOlder ? 1 : 0.75 + 0.25 * progress})`,
      }}
      className={cn(
        "absolute top-2 left-1/2 z-20 pointer-events-none",
        "size-9 rounded-full",
        "bg-background border border-border shadow-md",
        "items-center justify-center text-primary",
        "transition-[opacity,transform] duration-150 ease-out motion-reduce:transition-none",
        isVisible ? "opacity-100" : "opacity-0 scale-75",
        FLEX,
      )}
    >
      <span className="sr-only">{t("loadingEarlier")}</span>
      {loadingOlder ? (
        <Loader2 className="size-4 animate-spin text-primary" />
      ) : (
        <svg
          className="size-5 -rotate-90"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <circle
            cx="12"
            cy="12"
            r={radius}
            stroke="currentColor"
            strokeWidth="2.5"
            className="text-muted/30"
            fill="none"
          />
          <circle
            cx="12"
            cy="12"
            r={radius}
            stroke="currentColor"
            strokeWidth="2.5"
            className="text-primary transition-[stroke-dashoffset] duration-75 ease-out"
            fill="none"
            strokeDasharray={circumference}
            strokeDashoffset={strokeDashoffset}
            strokeLinecap="round"
          />
        </svg>
      )}
    </div>
  );
}

// Cold-load spinner — only when this thread has no items yet AND a fetch
// is in flight (no cache, no live bucket). A warm switch or a background
// refetch keeps its items on screen and never shows this.
export function ColdLoadSpinner({ show }: { show: boolean }) {
  if (!show) return null;
  return (
    <div className={cn("absolute inset-0 z-10 items-center justify-center gap-2 text-xs text-muted-foreground pointer-events-none", FLEX)}>
      <Loader2 className="size-4 animate-spin" />
      Loading conversation…
    </div>
  );
}

// Scroll-to-bottom arrow button — appears whenever the viewport is
// measured away from the bottom; clicking smooth-scrolls back.
export function ScrollToBottomButton({
  atBottom,
  onClick,
}: {
  atBottom: boolean;
  onClick: () => void;
}) {
  const t = useTranslations("timeline");
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={t("scrollToBottom")}
      className={cn(
        "absolute bottom-4 left-1/2 -translate-x-1/2 z-30",
        "size-9 rounded-full",
        // Solid bg, no backdrop-blur: backdrop-filter over the scroll
        // area forces iOS WebKit to re-sample the content behind it,
        // a known repaint-cost / blank-tile aggravator during streaming.
        "bg-background border border-border",
        "shadow-md hover:shadow-lg",
        "items-center justify-center",
        "text-muted-foreground hover:text-foreground",
        "transition-all duration-300 ease-out",
        atBottom
          ? "opacity-0 translate-y-2 pointer-events-none scale-75"
          : "opacity-100 translate-y-0 pointer-events-auto scale-100",
          FLEX
      )}
    >
      <ArrowDown className="size-4" />
    </button>
  );
}
