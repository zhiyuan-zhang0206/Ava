"use client";

import { X } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useEffect, useRef } from "react";

import { ContextMeter, useContextMeterWidthClass } from "@/components/inspector/context-meter";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";

// The live meter stays in the initial bundle; breakdown content loads on expand.
const ContextBreakdownBody = dynamic(
  () => import("./context-breakdown-body").then((module) => module.ContextBreakdownBody),
  { loading: () => <div className="h-4" aria-busy="true" /> },
);

export interface ContextButtonProps {
  /** The active agent, or null. Without one there is nothing to break down, so
   * the readout renders non-interactively. */
  agentId: number | null;
  /** Controlled expansion state. The composer owns it (single popover state)
   * so at most one of its upward popovers — this panel / the slash-command
   * dropdown — is open at a time: opening one closes the other. */
  open: boolean;
  onOpenChange: (open: boolean) => void;
  contextTokens: number;
  maxContextTokens: number;
  softCompactTokens: number;
  hardCompactTokens: number;
  showText?: boolean;
}

/** The collapsed readout's numeric text hides below sm (QA sweep 2026-09-18
 *  F3): at 390px the composer meta row is shared with the upload button and
 *  the Details control, and the text truncated to "Co". The gauge stays — its
 *  aria-label carries used/max/soft/hard — and the popup shows the numbers in
 *  full. */
const READOUT_TEXT_CLASS = "hidden sm:inline";

/** The composer's context readout as a button: the inline `ContextMeter` gauge
 * that, when clicked, expands the breakdown panel in place (lazy-loaded on
 * expand).
 *
 * Deliberately NOT a modal: the panel is absolutely positioned against the
 * composer's meta row (composer.tsx marks that row `relative` as the anchor —
 * same contract as SlashAutocomplete's dropdown) and grows *upward*
 * (`bottom-full`) over the bottom of the timeline. The composer below stays
 * fully visible and interactive, nothing reflows, and the rest of the page is
 * never blocked. Escape, an outside click, the close button, or clicking the
 * meter again collapses it. The open state is controlled by the composer
 * (single popover state — mutually exclusive with the slash dropdown). */
export function ContextButton(props: ContextButtonProps) {
  const { agentId, open, onOpenChange, ...meter } = props;
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const barWidthClassName = useContextMeterWidthClass();
  const t = useTranslations("contextBreakdown");

  // Escape / outside pointer-down collapse the panel. Document-level listeners
  // exist only while open; the closing outside click still lands on its target
  // (non-modal — nothing is swallowed). An Escape a lower layer already
  // consumed (defaultPrevented — e.g. the composer closing the slash dropdown)
  // is skipped so one keypress never closes two layers.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) onOpenChange(false);
    };
    const onPointerDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (panelRef.current?.contains(t) || buttonRef.current?.contains(t)) return;
      onOpenChange(false);
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open, onOpenChange]);

  if (agentId == null) {
    return <ContextMeter {...meter} barWidthClassName={barWidthClassName} textClassName={READOUT_TEXT_CLASS} />;
  }

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        data-testid="context-meter-button"
        onClick={() => onOpenChange(!open)}
        aria-expanded={open}
        className={cn("rounded-sm text-left hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring", MIN_W_0)}
      >
        <ContextMeter {...meter} barWidthClassName={barWidthClassName} textClassName={READOUT_TEXT_CLASS} />
      </button>
      {open ? (
        // bottom-full anchors the panel's bottom to the meta row's top, so it
        // can only occupy space *above* the composer (over the timeline). The
        // max-height + internal scroll keep a tall breakdown inside the
        // viewport instead of growing past it. Width is shrink-to-fit
        // (no `w-*`): the panel sizes to its content — min-w-80 is a floor so
        // a sparse breakdown isn't a sliver, max-w-[...] a ceiling so one
        // pathologically long category/section name can't push the panel
        // past the viewport. Inside that range nothing truncates; only past
        // the ceiling do the `truncate` rows below ellipsis.
        <div
          ref={panelRef}
          role="region"
          aria-label={t("title")}
          data-testid="context-breakdown-panel"
          // overflow-x-hidden is load-bearing, not decorative: per the CSS
          // overflow spec, an element with overflow-y other than "visible"
          // has its overflow-x computed value forced from "visible" to
          // "auto" too — the two axes can't be set independently that way —
          // so overflow-y-auto alone silently made this panel horizontally
          // scrollable once content ran past the max-width ceiling, instead
          // of the intended pure vertical scroll with only the ceiling-
          // exceeding rows truncating in place.
          //
          // whitespace-normal resets an inherited `white-space: nowrap`: the
          // composer's meta row wraps this panel in a `truncate` span (for
          // the *collapsed* meter's own text) — nowrap is an inherited
          // property, so without this reset every paragraph in here that
          // isn't itself `truncate` (the summary line, the description)
          // would refuse to wrap and get silently clipped by overflow-x-
          // hidden instead of flowing across lines like the rest of the
          // panel.
          className="absolute bottom-full left-0 z-50 mb-2 min-w-80 max-w-[min(28rem,90vw)] max-h-[50vh] overflow-x-hidden overflow-y-auto whitespace-normal rounded-md border border-border bg-popover p-2.5 text-popover-foreground shadow-md"
        >
          <div className={cn("mb-2 items-start justify-between gap-2", FLEX)}>
            <p className="text-sm font-semibold">{t("title")}</p>
            <button
              type="button"
              data-testid="context-breakdown-close"
              aria-label={t("close")}
              onClick={() => onOpenChange(false)}
              className="shrink-0 rounded-sm p-0.5 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <X className="size-3.5" />
            </button>
          </div>
          <ContextBreakdownBody agentId={agentId} />
        </div>
      ) : null}
    </>
  );
}
