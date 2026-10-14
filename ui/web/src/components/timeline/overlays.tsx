"use client";

// Overlays follow their owning interaction without intercepting pointer input.

import { ArrowDown, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/format/utils";
import { FLEX, OVERFLOW_HIDDEN } from "@/lib/layout/layout";

// The circle is clipped above the revealed native scroll space. No visible
// instructions compete with the conversation; the label remains accessible.
export function LoadOlderSpinner({ loadingOlder, pullProgress = 0, pullOffset = 0, returning = false }: {
  loadingOlder: boolean; pullProgress?: number; pullOffset?: number; returning?: boolean;
}) {
  const t = useTranslations("timeline");
  const visible = loadingOlder || pullOffset > 0;
  const state = loadingOlder ? "loading" : pullProgress >= 1 ? "ready" : "pulling";
  const height = pullOffset || (loadingOlder ? 40 : 0);
  const circumference = 2 * Math.PI * 8;
  const arc = loadingOlder ? 0.72 : 0.18 + 0.54 * pullProgress;
  return (
    <div
      role="status"
      aria-hidden={!visible}
      data-testid="load-older-spinner"
      data-state={visible ? state : "idle"}
      data-pull-offset={pullOffset}
      style={{ height }}
      className={cn(
        "absolute top-11 inset-x-0 z-20 pointer-events-none text-muted-foreground justify-center items-center",
        returning && "transition-[height,opacity] duration-200 ease-out motion-reduce:transition-none",
        visible ? "opacity-100" : "opacity-0",
        OVERFLOW_HIDDEN, FLEX,
      )}
    >
      {visible ? (
        <svg viewBox="0 0 24 24" className="size-5 shrink-0 animate-spin motion-reduce:animate-none" aria-hidden="true">
          <circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" strokeWidth="1.75" opacity="0.15" />
          <circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round"
            strokeDasharray={`${circumference * arc} ${circumference * (1 - arc)}`} />
        </svg>
      ) : null}
      <span className="sr-only">{t("loadingEarlier")}</span>
    </div>
  );
}

// Gesture paging remains available to keyboard and assistive-technology users.
// Reveal this control only when focused, without adding idle reading chrome.
export function LoadOlderKeyboardButton({ loading, onClick }: { loading: boolean; onClick: () => boolean }) {
  const t = useTranslations("timeline");
  return (
    <button
      type="button"
      data-testid="load-older-keyboard"
      disabled={loading}
      onClick={onClick}
      className="sr-only focus:not-sr-only focus:absolute focus:top-14 focus:left-1/2 focus:-translate-x-1/2 focus:z-30 focus:rounded-md focus:border focus:bg-background focus:px-3 focus:py-2 focus:text-xs"
    >
      {t("loadEarlier")}
    </button>
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
  refreshing = false,
  onClick,
}: {
  atBottom: boolean;
  refreshing?: boolean;
  onClick: () => void;
}) {
  const t = useTranslations("timeline");
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={t("scrollToBottom")}
      aria-busy={refreshing}
      data-testid="scroll-to-bottom"
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
        atBottom && !refreshing
          ? "opacity-0 translate-y-2 pointer-events-none scale-75"
          : "opacity-100 translate-y-0 pointer-events-auto scale-100",
          FLEX
      )}
    >
      {refreshing ? (
        <>
          <svg data-testid="latest-loading-ring" viewBox="0 0 36 36" aria-hidden="true"
            className="absolute inset-0 size-full animate-spin motion-reduce:animate-none pointer-events-none">
            <circle cx="18" cy="18" r="16.5" fill="none" stroke="currentColor" strokeWidth="1.5"
              strokeDasharray="38 66" strokeLinecap="round" />
          </svg>
          <span role="status" className="sr-only">{t("loadingLatest")}</span>
        </>
      ) : null}
      <ArrowDown data-testid="scroll-to-bottom-arrow" className="size-4" />
    </button>
  );
}
