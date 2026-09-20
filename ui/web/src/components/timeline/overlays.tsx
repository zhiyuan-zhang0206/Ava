"use client";

// Floating overlay chrome for the timeline view: the load-older spinner, the
// cold-load spinner, and the scroll-to-bottom button. (The load-earlier
// button, the in-divider pill and the pull-to-load ring are gone — reaching
// the top auto-loads older pages, task #4186.)

import { ArrowDown, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";
import { FLEX, FLEX_1 } from "@/lib/layout";

// Load-older spinner — shown while an automatic scroll-up paging fetch (the
// view settled at the top and older pages remain) is in flight. Always
// mounted (opacity-only visibility) and an absolutely-positioned overlay
// outside the scrolled content, so it never shifts scrollHeight or disturbs
// the prepend anchor; pointer-events-none in every state so it can never
// capture the wheel/touch gesture that triggered it.
export function LoadOlderSpinner({ loadingOlder }: { loadingOlder: boolean }) {
  const t = useTranslations("timeline");
  return (
    <div
      role="status"
      aria-label={t("loadingEarlier")}
      aria-hidden={!loadingOlder}
      data-testid="load-older-spinner"
      className={cn(
        "absolute top-14 left-1/2 -translate-x-1/2 z-20 pointer-events-none",
        "size-9 rounded-full",
        "bg-background border border-border shadow-md",
        "items-center justify-center text-primary",
        "transition-[opacity,transform] duration-150 ease-out motion-reduce:transition-none",
        loadingOlder ? "opacity-100" : "opacity-0 scale-75",
        FLEX,
      )}
    >
      <span className="sr-only">{t("loadingEarlier")}</span>
      <Loader2 className="size-4 animate-spin text-primary" />
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

// The divider's dashed rule: longer dashes and gaps than the browser's
// `border-dashed` (~2-3px) at a 1:1 ratio, and a deeper color than
// `border-border` so the line is legible in the dark theme (user feedback
// 2026-09-17, task #3870). currentColor lets `text-*` carry the tone.
const DIVIDER_RULE_CLASS =
  "h-px bg-[repeating-linear-gradient(to_right,currentColor_0_6px,transparent_6px_12px)] text-muted-foreground/60";

// Segment divider — a pure label, never a control: rank 0 marks the live
// boundary between retained history and the current post-compact segment
// (task #3698); the historical ranks carry the scroll-back copy. The label
// carries no arrow glyph (task #3870); older history loads by scrolling to
// the top (task #4186).
export function CompactHistoryDivider({ rank }: { readonly rank: number }) {
  const t = useTranslations("timeline");
  const label = rank === 0 ? t("compactBoundaryDivider") : t("compactHistoryDivider");
  return (
    <div
      data-testid="compact-history-divider"
      data-segment-rank={rank}
      aria-live="off"
      className={cn("items-center gap-2 py-1 text-[11px] text-muted-foreground/70", FLEX)}
    >
      <span aria-hidden="true" className={cn(DIVIDER_RULE_CLASS, FLEX_1)} />
      <span className="shrink-0">{label}</span>
      <span aria-hidden="true" className={cn(DIVIDER_RULE_CLASS, FLEX_1)} />
    </div>
  );
}
