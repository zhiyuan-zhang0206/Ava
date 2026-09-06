"use client";

// Floating overlay chrome for the timeline view: the load-older hint, the
// cold-load spinner, and the scroll-to-bottom button. Split out of index.tsx
// (outlier cleanup, task #1010); behavior is identical.

import { ArrowDown, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";
import { FLEX } from "@/lib/layout";

// Older-history loading hint — overlay (not in flow) so it does not
// shift scrollHeight and disturb prepend anchoring. Always mounted (so
// the opacity transition below has something to animate) but always
// pointer-events-none: it is purely a status indicator with nothing
// clickable inside it, and while visible it sits directly over the
// scroll viewport at the exact spot the user is scrolling through to
// reach older history — capturing pointer/wheel events there would
// create a dead strip for the gesture that triggered it in the first
// place. role="status" + aria-hidden ties its accessibility-tree
// presence to loadingOlder so assistive tech does not encounter
// "Loading earlier messages…" while idle.
export function LoadingOlderBadge({ loadingOlder }: { loadingOlder: boolean }) {
  return (
    <div
      role="status"
      aria-hidden={!loadingOlder}
      className={cn(
        "absolute top-2 left-1/2 -translate-x-1/2 z-10 pointer-events-none",
        "items-center gap-1.5 px-2.5 py-1 rounded-full",
        "bg-background border border-border shadow-sm",
        "text-xs text-muted-foreground",
        "transition-opacity duration-200",
        loadingOlder ? "opacity-100" : "opacity-0",
        FLEX
      )}
    >
      <Loader2 className="size-3 animate-spin" />
      Loading earlier messages…
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
