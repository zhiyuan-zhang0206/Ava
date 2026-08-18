"use client";

// R4 layer 4 — the ONE breakpoint source (Task #1024, design v0.6 §5.5).
// Every responsive decision derives from useBreakpoint(); no second mobile
// implementation (invariant 4). Three mechanisms (useMediaQuery + prop
// branches / CSS media queries + dual always-mounted components / in-component
// compact branches) converge to one: breakpoint + conditional render.
//
// Tiers — 320 (smallest supported phone) / 390 (typical phone, the #979
// precedent) / 768 (Tailwind md — the narrow/full-width boundary the timeline
// and sidebar already use) / lg (1024 — the desktop split boundary fleet and
// the inspector use). 320 and 390 are also the e2e layout-test tiers (I1–I6).

import { useEffect, useState } from "react";

import { useMediaQuery } from "./use-media-query";

export const BREAKPOINT_XS_PX = 320;
export const BREAKPOINT_SM_PX = 390;
export const BREAKPOINT_MD_PX = 768;
export const BREAKPOINT_LG_PX = 1024;

export type BreakpointTier = "xs" | "sm" | "md" | "lg" | "xl";

/** Tier of a viewport width: xs <320, sm 320–389, md 390–767, lg 768–1023, xl ≥1024. */
export function tierForWidth(width: number): BreakpointTier {
  if (width < BREAKPOINT_XS_PX) return "xs";
  if (width < BREAKPOINT_SM_PX) return "sm";
  if (width < BREAKPOINT_MD_PX) return "md";
  if (width < BREAKPOINT_LG_PX) return "lg";
  return "xl";
}

export interface Breakpoint {
  /** Current tier by viewport width; "xs" before mount (SSR/mobile-safe default). */
  tier: BreakpointTier;
  /** Viewport below md (768px): full-width timeline/composer, drawer sidebar, full-screen inspector. */
  isNarrow: boolean;
  /** Viewport at or above lg (1024px): side-by-side fleet split, floating inspector overlay. */
  isLarge: boolean;
}

/** SSR-safe single breakpoint source. Defaults to the narrow/mobile layout
 *  before mount, then syncs to the real viewport (same contract useMediaQuery
 *  had: a desktop load paints mobile for one frame — accepted everywhere). */
export function useBreakpoint(): Breakpoint {
  const isNarrow = !useMediaQuery(`(min-width: ${BREAKPOINT_MD_PX}px)`);
  const isLarge = useMediaQuery(`(min-width: ${BREAKPOINT_LG_PX}px)`);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR-safe sync: read the real viewport once after mount
    setWidth(window.innerWidth);
    const onResize = () => setWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return { tier: tierForWidth(width), isNarrow, isLarge };
}
