"use client";

// Floating overlay chrome for the timeline view: the pull-to-load indicator,
// the load-older fallback control, the cold-load spinner, and the
// scroll-to-bottom button.

import { useEffect, useRef } from "react";
import { ArrowDown, ArrowUp, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";
import { FLEX, FLEX_1 } from "@/lib/layout";

export interface PullToLoadIndicatorProps {
  pullDistance: number;
  pullThreshold?: number;
  loadingOlder: boolean;
  /** Task #3932: the load control is embedded in the topmost divider row and
   *  carries the loading feedback itself (pill spinner) — the ring then serves
   *  the pull gesture only, staying hidden while pullDistance is 0. Task
   *  #3934: while the control is inline, the ring's band also clears that row
   *  so the gesture sweep never crosses the pill. */
  inlineLoadControl?: boolean;
}

// Pull-down-to-load indicator — circular progress ring that fills up as the user
// pulls down at the top of the timeline viewport. When filled and released, it
// enters the loadingOlder state (spinning loader) while older compact history is
// fetched. Floating overlay (not in flow) to guarantee zero layout shift / jitter.
export function PullToLoadIndicator({
  pullDistance,
  pullThreshold = 56,
  loadingOlder,
  inlineLoadControl = false,
}: PullToLoadIndicatorProps) {
  const t = useTranslations("timeline");
  const progress = Math.min(1, Math.max(0, pullDistance / pullThreshold));
  // With the control inline (task #3932), the pill is the loading feedback —
  // the ring would stack a second spinner on the same spot, so it surfaces
  // only while the pull gesture itself is in progress.
  const isVisible = pullDistance > 0 || (loadingOlder && !inlineLoadControl);
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
        "absolute left-1/2 z-20 pointer-events-none",
        // Task #3934: with the control inline (task #3932) the top-14 band
        // hosts the pill — the ring clears that row instead, starting at its
        // bottom edge (88px: pill band 56-84 + the row's py-1).
        inlineLoadControl ? "top-22" : "top-14",
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

// Load-older fallback control — a real button shown when the user settles
// at the top with older history remaining. Keyboard / screen-reader /
// scrollbar-drag users fire scroll events only (never wheel/touch), so this
// is their path to older history — and it doubles as the discoverability
// hint for everyone, since the pull ring only appears mid-pull. Mounted
// always (for the opacity transition) but unfocusable and pointer-events-none
// while hidden, so an invisible control can never trap keyboard focus.
// Position (task #3224②): inside the floating header's band (top-2) at
// z-30 — pinned there so the button never overlaps the conversation content.
// Placed below the header (the previous top-14) it collided with the first
// turn block's header band at the top settle (QA-measured 26.5px overlap).
// Stacking, from the two historical constraints: ABOVE the translucent
// header (z-20 — otherwise it overlays the button and intercepts its pointer
// events) and above the stuck turn header (#1954, sticky top-11 z-10 — z-20
// already lifted the button over it; z-30 keeps that and additionally wins
// the header bar). The bar's center band carries no interactive content, so
// while visible the button stays the click target; hidden, it is
// pointer-events-none + unfocusable, so nothing is trapped.
export function LoadOlderButton({
  visible,
  onClick,
}: {
  visible: boolean;
  onClick: () => void;
}) {
  const t = useTranslations("timeline");
  const buttonRef = useRef<HTMLButtonElement>(null);
  // When the control hides while it still holds focus (user scrolled away via
  // wheel/touch, a load started, hasMoreOlder flipped), release the focus —
  // an invisible focused button would still activate on Enter/Space.
  useEffect(() => {
    if (!visible && buttonRef.current && buttonRef.current === document.activeElement) {
      buttonRef.current.blur();
    }
  }, [visible]);
  return (
    <div
      className={cn(
        "absolute top-2 left-1/2 -translate-x-1/2 z-30",
        "transition-opacity duration-200",
        visible ? "opacity-100" : "opacity-0 pointer-events-none",
      )}
    >
      <button
        ref={buttonRef}
        type="button"
        onClick={onClick}
        tabIndex={visible ? 0 : -1}
        aria-hidden={!visible}
        data-testid="load-older-button"
        className={cn(
          "items-center gap-1.5 px-2.5 py-1 rounded-full",
          "bg-background border border-border shadow-sm",
          "text-[11px] text-muted-foreground hover:text-foreground",
          "focus-visible:ring-[3px] focus-visible:ring-ring/50",
          FLEX,
        )}
      >
        <ArrowUp className="size-3" />
        {t("loadEarlier")}
      </button>
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

export function CompactHistoryDivider({
  rank,
  onLoadOlder,
  loadingOlder = false,
}: {
  readonly rank: number;
  /** Present when this divider is the list's topmost row and older pages
   *  remain: the row then carries the load-earlier control itself, so the
   *  boundary and the loading affordance are ONE entry — instead of a
   *  floating "Load earlier messages" button stacked right above a divider
   *  whose copy reads like the same offer (task #3932). */
  readonly onLoadOlder?: () => void;
  readonly loadingOlder?: boolean;
}) {
  const t = useTranslations("timeline");
  // rank 0 = the live boundary between retained history and the current
  // post-compact segment (task #3698); the historical ranks keep the
  // scroll-back copy. As a pure label the rule carries no glyph (removed per
  // the 2026-09-17 report, task #3870); as the load control it adopts the
  // load-earlier button's arrow + pill treatment.
  const label = rank === 0 ? t("compactBoundaryDivider") : t("compactHistoryDivider");
  if (onLoadOlder === undefined) {
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
  return (
    <div
      data-testid="compact-history-divider"
      data-segment-rank={rank}
      data-load-control="true"
      aria-live="off"
      className={cn("items-center gap-2 py-1 text-[11px] text-muted-foreground/70", FLEX)}
    >
      <span aria-hidden="true" className={cn(DIVIDER_RULE_CLASS, FLEX_1)} />
      <button
        type="button"
        data-testid="load-older-divider"
        onClick={onLoadOlder}
        disabled={loadingOlder}
        className={cn(
          "items-center gap-1.5 shrink-0 px-2.5 py-1 rounded-full",
          "bg-background border border-border shadow-sm",
          "text-[11px] text-muted-foreground hover:text-foreground",
          "focus-visible:ring-[3px] focus-visible:ring-ring/50",
          FLEX,
        )}
      >
        {loadingOlder ? (
          <Loader2 className="size-3 animate-spin text-primary" />
        ) : (
          <ArrowUp className="size-3" />
        )}
        {t("loadEarlier")}
      </button>
      <span aria-hidden="true" className={cn(DIVIDER_RULE_CLASS, FLEX_1)} />
    </div>
  );
}
