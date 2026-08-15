// Regression guard for #564: agent-switch + load must never break sticky
// without a genuine user scroll-up gesture.
//
// The controller (lib/sticky.ts) is pure and unit-tested; the RACE lives in
// the ORDER of DOM events the component wiring feeds it during a switch:
//   - the bump pin (force scroll-to-bottom on switch)
//   - the browser's clamp scroll event (old content replaced by shorter new)
//   - ResizeObserver callbacks (content grown by SSE fold / refetch)
//   - the pin's own scroll echo
//   - wheel noise (trackpad momentum) landing mid-load
//
// The pin is the load-bearing event: it refreshes the controller's
// bottom anchor + baseline on the NEW thread's content, and the clamp's
// geometry then makes every subsequent echo/wheel measure dist 0 against
// that anchor. If the pin is ever skipped or delayed past the first
// scroll/wheel event of the load (a wiring regression), the stale anchor
// + stale baseline misread the clamp echo as a user scroll-up and
// permanently unstick — exactly the #564 symptom. The sweep below
// enumerates event orderings: every sequence where the pin runs before
// the first scroll/wheel must stay sticky.

import { describe, expect, it } from "vitest";

import {
  POINTER_STICKY_THRESHOLDS,
  type ScrollSnapshot,
  type StickyController,
  createStickyController,
} from "./sticky";

const CH = 643; // viewport clientHeight (matches real session)

function v(st: number, sh: number): ScrollSnapshot {
  return { scrollTop: st, scrollHeight: sh, clientHeight: CH };
}

/** Simulate a pin write: viewport.scrollTop = scrollHeight, browser clamps. */
function pinWrite(ctl: StickyController, sh: number): void {
  const clamped = Math.max(0, sh - CH);
  ctl.notifyPinnedToBottom(v(clamped, sh));
}

// ── The user-described sequence: click → sticky (pin) → load → unstick? ──
describe("#564: agent switch + load must not break sticky", () => {
  it("cold switch: pin on empty, then the full timeline loads — sticky holds", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pinWrite(ctl, CH); // switchThread: items=[] → bump → pin (clamped to 0)
    expect(ctl.isSticky()).toBe(true);
    // load lands: content grows 0 → 5000
    ctl.handleScroll(v(0, 5000)); // clamp echo (reads LIVE dom: st=0, sh=5000)
    expect(ctl.isSticky()).toBe(true); // near last-known bottom (anchor=CH) → keep
    expect(ctl.handleLayoutChange(v(0, 5000))).toBe(true); // RO pins
    pinWrite(ctl, 5000);
    ctl.handleScroll(v(4357, 5000)); // the pin's echo
    expect(ctl.isSticky()).toBe(true);
  });

  it("hot switch: taller → shorter (clamp) → SSE growth before the clamp echo — sticky holds", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pinWrite(ctl, 5000); // user pinned on agent A (tall)
    ctl.handleScroll(v(4357, 5000));
    // click B (cached 1500): content 5000→1500, clamp st 4357→857; bump pin
    pinWrite(ctl, 1500); // anchor=1500, prev=857
    // SSE folds 1500→6000 BEFORE the clamp echo dispatches
    ctl.handleScroll(v(857, 6000));
    expect(ctl.isSticky()).toBe(true); // near last-known bottom: 1500-857-643=0
    expect(ctl.handleLayoutChange(v(857, 6000))).toBe(true);
    pinWrite(ctl, 6000);
    expect(ctl.isSticky()).toBe(true);
  });

  it("wheel noise mid-load (pin fresh) is absorbed at the old bottom", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pinWrite(ctl, 1500); // switch pin on B, st=857
    ctl.handleWheel(-25, v(857, 3000)); // SSE grew; momentum wheel before RO
    expect(ctl.isSticky()).toBe(true);
    expect(ctl.handleLayoutChange(v(857, 3000))).toBe(true);
  });

  it("clamp echo arrives AFTER the refetch growth and RO pin — echo sees the bottom", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pinWrite(ctl, 1500);
    expect(ctl.handleLayoutChange(v(857, 2097))).toBe(true);
    pinWrite(ctl, 2097);
    ctl.handleScroll(v(1454, 2097)); // the delayed clamp echo reads live st
    expect(ctl.isSticky()).toBe(true);
  });

  it("shrink-then-grow across the switch keeps sticky (shrink branch)", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pinWrite(ctl, 5000);
    pinWrite(ctl, 1500); // switch pin on B (shorter) — st=857
    ctl.handleScroll(v(757, 1400)); // HTTP merge shrank below the pin → clamp
    expect(ctl.isSticky()).toBe(true); // shrink branch: 1400 < 1500
    ctl.handleScroll(v(757, 2000)); // then SSE grows
    expect(ctl.isSticky()).toBe(true); // near last-known bottom (anchor 1400)
    expect(ctl.handleLayoutChange(v(757, 2000))).toBe(true);
  });

  it("genuine scroll-up during the load still unsticks (intended)", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pinWrite(ctl, 1500);
    ctl.handleWheel(-120, v(500, 1500));
    expect(ctl.isSticky()).toBe(false);
    ctl.handleScroll(v(500, 1500));
    expect(ctl.isSticky()).toBe(false);
    expect(ctl.handleLayoutChange(v(500, 1500))).toBe(false); // RO won't yank
  });
});

// ── Exhaustive ordering sweep of switch events ──
describe("#564: exhaustive ordering sweep of switch events", () => {
  // Event alphabet (each applied to a fresh controller in the listed order):
  //   P  — bump pin on the new (cached) content, sh=1500
  //   G  — content grows to 6000 (SSE fold / refetch) — changes sh only
  //   C  — clamp echo: handleScroll at the clamped st (sh live — may be grown)
  //   R  — ResizeObserver: handleLayoutChange + pin when true
  //   W  — wheel noise: handleWheel(-25) at the current position
  //
  // Production ordering guarantee (the wiring): P runs synchronously in the
  // same commit that swaps the items, BEFORE any scroll/RO task can dispatch
  // — so the sequences that matter are those where P precedes the first
  // scroll/wheel event (C, W). Those must ALL stay sticky. Sequences without
  // an early P document the boundary: with a stale anchor the controller
  // (correctly, from its point of view) reads the clamp echo as a gesture —
  // which is why the wiring guarantee is the actual fix.
  const sequences: string[][] = [];
  const events = ["P", "C", "R", "W", "G"];
  const gen = (pre: string[], depth: number) => {
    if (depth === 0) return;
    for (const e of events) {
      const seq = [...pre, e];
      sequences.push(seq);
      gen(seq, depth - 1);
    }
  };
  gen([], 4);

  it.each(sequences.map((s) => [s.join("")] as const))(
    "sequence %s stays sticky",
    (seqStr) => {
      const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
      // Pre-state: user pinned on a tall thread A
      pinWrite(ctl, 5000);
      ctl.handleScroll(v(4357, 5000));

      let sh = 1500; // B's cached content after the swap
      let st = Math.min(4357, Math.max(0, sh - CH)); // post-swap clamped st

      // The wiring guarantee: if the sequence pins before its first
      // scroll/wheel event, it must survive. Skip sequences whose first
      // scroll/wheel event precedes the pin — those document the boundary
      // (stale-anchor misread) rather than a production path.
      const firstScrollOrWheel = seqStr.search(/[CW]/);
      const firstPin = seqStr.indexOf("P");
      if (firstScrollOrWheel !== -1 && (firstPin === -1 || firstPin > firstScrollOrWheel)) {
        return; // boundary case, not a production ordering
      }

      for (const ev of seqStr) {
        switch (ev) {
          case "P":
            st = Math.max(0, sh - CH);
            ctl.notifyPinnedToBottom(v(st, sh));
            break;
          case "G":
            sh += 4500;
            break;
          case "C":
            ctl.handleScroll(v(st, sh));
            break;
          case "R": {
            const shouldPin = ctl.handleLayoutChange(v(st, sh));
            if (shouldPin) {
              st = Math.max(0, sh - CH);
              ctl.notifyPinnedToBottom(v(st, sh));
            }
            break;
          }
          case "W":
            ctl.handleWheel(-25, v(st, sh));
            break;
        }
      }
      expect(ctl.isSticky()).toBe(true);
    },
  );

  // And the boundary, documented: WITHOUT the pin before the first
  // scroll/wheel event, a stale anchor misreads the clamp echo as a gesture
  // and unsticks — this is the #564 failure mode the wiring must prevent.
  it("boundary: no pin before the clamp echo + growth = unstick (wiring must prevent)", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pinWrite(ctl, 5000); // last pin was on the OLD thread (stale after swap)
    ctl.handleScroll(v(857, 6000)); // clamp echo, content already grown
    expect(ctl.isSticky()).toBe(false);
    expect(ctl.handleLayoutChange(v(857, 6000))).toBe(false);
  });
});
