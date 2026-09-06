// Shared layout contract for the Ava frontend — the single source of
// truth for the persistent-bar geometry AND the named layout invariants
// (I1–I6) that guard it.
//
// Two consumers:
//   1. Components import the PRIMITIVES (FLEX / MIN_W_0 / …) instead of
//      writing these classes by hand — eslint-rules/layout-primitive.mjs
//      forbids raw writes, so a layout redesign becomes "change one
//      primitive's value" and every consumer follows.
//   2. Tests import LAYOUT_INVARIANTS so the jsdom class-contract layer
//      (ui/web/src/app/page.test.tsx + fleet tests) and the real-engine
//      Playwright layer (tests/e2e/test_layout_invariants.py) share one
//      named checklist: every invariant has a jsdom contract AND an
//      engine assertion at the 320 / 390 / 768 viewport tiers.
//
// Why these six classes are "contract" material (not ordinary utilities):
// each one has broken the mobile layout as a real P0 — #874 (missing
// display:flex → timeline could not scroll vertically) and #979 (missing
// min-w-0 → 312px unreachable right clip on 390px) are the same flex-item
// contract failing on two axes. jsdom cannot measure layout, so the
// classes are the jsdom half of the two-layer defense and must stay
// written in one place.

// Persistent bars sit side-by-side or stack visually, so a single shared
// height keeps their bottom borders pixel-aligned regardless of what each
// bar's content needs. Single source of truth for the bar height in px.
// BAR_HEIGHT_CLASS must stay 1:1 with this number (h-11 = 2.75rem = 44px);
// keeping the px value named lets consumers derive offsets from it instead
// of re-hardcoding the class string.
export const BAR_HEIGHT_PX = 44;
export const BAR_HEIGHT_CLASS = "h-11";

// Each home column owns a separate title divider. Insetting its endpoints
// prevents the Agent Tree, Timeline, and Inspector rules from reading as one
// page-wide line while preserving their shared vertical position.
export const BAR_DIVIDER_CLASS =
  "relative after:pointer-events-none after:absolute after:inset-x-1 after:bottom-0 after:h-px after:bg-border";

// Top padding that clears the floating HeaderBar for content scrolling
// beneath it (timeline content column, user ruling 2026-08-06). 56px =
// BAR_HEIGHT_PX + 8px breathing room — NOT an independent value: change
// BAR_HEIGHT_PX / BAR_HEIGHT_CLASS and this must move with it, or the
// first content row slides under the bar (the #874/#979 layout-regression
// class).
export const BAR_CLEAR_TOP_PADDING_CLASS = "pt-[52px]";

// ── Flex-contract primitives (the I1–I6 material) ──────────────────────
// These six classes are the layout invariants' raw material. Every usage
// goes through these constants — eslint forbids writing them by hand in a
// className string. Tailwind's source scanner picks the literal class
// names up from this file, so the utilities are generated exactly as
// before.
export const FLEX = "flex"; // display:flex — the I6 vertical chain's root
export const FLEX_1 = "flex-1"; // grow/shrink/basis 1 — surface fills its pane
export const FLEX_COL = "flex-col"; // column direction — the scroll axis
export const MIN_W_0 = "min-w-0"; // horizontal flex-item contract (#979)
export const MIN_H_0 = "min-h-0"; // vertical flex-item contract (#874)
export const OVERFLOW_HIDDEN = "overflow-hidden"; // clip, never widen

// ── The named invariants (I1–I6) ───────────────────────────────────────
// One entry per invariant; `pages` says which page each applies to. Tests
// iterate this list so the jsdom layer and the Playwright layer can never
// silently cover different invariant sets.
export const LAYOUT_VIEWPORT_TIERS = [320, 390, 768] as const;

export interface LayoutInvariant {
  /** Invariant id, I1–I6. */
  readonly id: string;
  /** One-line name. */
  readonly name: string;
  /** What the invariant asserts (asserted in a real engine). */
  readonly assertion: string;
  /** Pages the invariant applies to. */
  readonly pages: readonly ("timeline" | "fleet")[];
}

export const LAYOUT_INVARIANTS: readonly LayoutInvariant[] = [
  {
    id: "I1",
    name: "No horizontal page scroll",
    assertion:
      "document.scrollingElement.scrollWidth <= clientWidth + 1 at 320/390/768",
    pages: ["timeline", "fleet"],
  },
  {
    id: "I2",
    name: "Timeline surface not wider than its parent",
    assertion:
      "timeline-surface scrollWidth <= parent section clientWidth (the #979 min-w-0 contract)",
    pages: ["timeline"],
  },
  {
    id: "I3",
    name: "Composer never overflows the viewport",
    assertion: "composer textarea bounding box fits inside the viewport width",
    pages: ["timeline"],
  },
  {
    id: "I4",
    name: "Fleet toolbar never widens the page",
    assertion: "toolbar/chips rows produce no page-level horizontal scroll",
    pages: ["fleet"],
  },
  {
    id: "I5",
    name: "Inbox rows never overflow their container",
    assertion: "each inbox row element width <= its container width",
    pages: ["fleet"],
  },
  {
    id: "I6",
    name: "Single main scroll region",
    assertion:
      "the page does not scroll as a whole — the min-h-0 flex chain routes scrolling into the surface",
    pages: ["timeline"],
  },
];

/** ids of every invariant — used by tests to assert full coverage. */
export const LAYOUT_INVARIANT_IDS = LAYOUT_INVARIANTS.map((i) => i.id);
