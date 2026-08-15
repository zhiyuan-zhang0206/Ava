import { describe, expect, it } from "vitest";

import {
  POINTER_STICKY_THRESHOLDS,
  TOUCH_STICKY_THRESHOLDS,
  type ScrollSnapshot,
  type StickyController,
  bottomZone,
  createStickyController,
  isAtBottom,
} from "./sticky";

// Convenience: build a snapshot. dist = scrollHeight - scrollTop - clientHeight.
function view(scrollTop: number, scrollHeight: number, clientHeight = 600): ScrollSnapshot {
  return { scrollTop, scrollHeight, clientHeight };
}

// Simulate the component's pin(): write scrollTop = scrollHeight (browser
// clamps to scrollHeight - clientHeight) and report back post-clamp.
function pin(ctl: StickyController, scrollHeight: number, clientHeight = 600): ScrollSnapshot {
  const clamped = Math.max(0, scrollHeight - clientHeight);
  const v = view(clamped, scrollHeight, clientHeight);
  ctl.notifyPinnedToBottom(v);
  return v;
}

describe("bottomZone (clamped bottomPxRatio * clientHeight)", () => {
  it("scales at 20% within the clamp window", () => {
    expect(bottomZone(700)).toBe(140); // 0.2 * 700
  });
  it("floors at 80 for tiny viewports", () => {
    expect(bottomZone(300)).toBe(80); // 0.2 * 300 = 60 → 80
  });
  it("caps at 200 for huge viewports", () => {
    expect(bottomZone(1500)).toBe(200); // 0.2 * 1500 = 300 → 200
  });
  it("pointer preset uses the tight clamp [24, 72]", () => {
    expect(bottomZone(600, POINTER_STICKY_THRESHOLDS)).toBe(30); // 0.05 * 600
    expect(bottomZone(300, POINTER_STICKY_THRESHOLDS)).toBe(24); // 0.05*300=15 → 24
    expect(bottomZone(2000, POINTER_STICKY_THRESHOLDS)).toBe(72); // 0.05*2000=100 → 72
  });
});

describe("isAtBottom (button visibility — pure position check)", () => {
  it("dist < bottomZone → at bottom (button hidden)", () => {
    // dist = 1000 - 350 - 600 = 50 < 120
    expect(isAtBottom(view(350, 1000))).toBe(true);
  });
  it("dist >= bottomZone → not at bottom (button shown)", () => {
    // dist = 2000 - 800 - 600 = 600 >= 120
    expect(isAtBottom(view(800, 2000))).toBe(false);
  });
  it("dist exactly === bottomZone → not at bottom (pins `<` not `<=`)", () => {
    // dist = 1000 - 280 - 600 = 120 === bottomZone(600)
    expect(isAtBottom(view(280, 1000))).toBe(false);
  });
  it("pointer preset: the same dist=50 is outside the tight zone", () => {
    expect(isAtBottom(view(350, 1000), POINTER_STICKY_THRESHOLDS)).toBe(false);
  });
});

describe("controller basics", () => {
  it("starts sticky (fresh timeline pins on mount)", () => {
    const ctl = createStickyController();
    expect(ctl.isSticky()).toBe(true);
    expect(ctl.handleLayoutChange(view(0, 100))).toBe(true);
  });

  it("user scroll-up beyond the zone unsticks; scrolling back to the bottom re-sticks", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 2000); // at bottom: scrollTop 1400
    // one fast upward flick: 1400 → 900 (drop 500 > 20; dist 500 >= 120)
    ctl.handleScroll(view(900, 2000));
    expect(ctl.isSticky()).toBe(false);
    // scroll back down into the zone: dist = 2000-1900-600 → clamped case, use 1390 → dist 10
    ctl.handleScroll(view(1390, 2000));
    expect(ctl.isSticky()).toBe(true);
  });

  it("small upward jitter (≤ unstickDeltaPx) away from the bottom keeps state", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 2000);
    // user scrolled up deliberately → unstuck
    ctl.handleScroll(view(900, 2000));
    expect(ctl.isSticky()).toBe(false);
    // 5px rebound up: no state change (stays unstuck)
    ctl.handleScroll(view(895, 2000));
    expect(ctl.isSticky()).toBe(false);
  });

  it("downward scroll toward (but not reaching) the bottom does not change state", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 3000);
    ctl.handleScroll(view(1000, 3000)); // unstick (drop 1400)
    expect(ctl.isSticky()).toBe(false);
    ctl.handleScroll(view(1500, 3000)); // halfway down, dist 900 — still unstuck
    expect(ctl.isSticky()).toBe(false);
  });
});

describe("programmatic scrolls are neutralized by baseline sync", () => {
  // The send-flow family (#11, #1238, #1297): a programmatic
  // scroll-to-bottom dispatches a scroll event the old design could
  // misread as a user scroll-up. Now every programmatic scroll reports
  // itself and moves the baseline with it, so the echo shows zero user
  // movement and decides nothing.

  it("the pin's own scroll event leaves sticky on", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    const pinned = pin(ctl, 2000); // send → pin; scrollTop 1400
    ctl.handleScroll(pinned); // the echo
    expect(ctl.isSticky()).toBe(true);
  });

  it("prepend compensation does not re-stick an unstuck reader", () => {
    // load-older: user reads history near the top; an older window
    // prepends and the component shifts scrollTop DOWN to keep their
    // place. The shift is not reported — the echo is downward, which the
    // position rule ignores, and it re-syncs the baseline itself.
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 2000);
    ctl.handleScroll(view(100, 2000)); // scrolled way up → unstuck
    expect(ctl.isSticky()).toBe(false);
    // older window (+1000px) prepends; component compensates 100 → 1100
    ctl.handleScroll(view(1100, 3000)); // the echo
    expect(ctl.isSticky()).toBe(false);
    // and the baseline healed: a later real scroll-up is judged from 1100
    ctl.handleScroll(view(1000, 3000));
    expect(ctl.isSticky()).toBe(false);
  });

  it("a user scroll that lands past the pin target is judged as user input", () => {
    // pin writes 1400; before the event dispatches the user wheels up 100
    // → the live read at dispatch reports 1300, 100px above the baseline.
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 2000);
    ctl.handleScroll(view(1300, 2000)); // dist 100 >= 30, drop 100 > 20
    expect(ctl.isSticky()).toBe(false);
  });

  it("re-sticks when the user scrolls back to the real bottom after unsticking", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 2000);
    ctl.handleScroll(view(900, 2000)); // user scroll → unstick
    expect(ctl.isSticky()).toBe(false);
    ctl.handleScroll(view(1400, 2000)); // back at the bottom → re-stick
    expect(ctl.isSticky()).toBe(true);
  });
});

describe("send flow: a clamp OBSERVED after a chunk grew (#1431 regression gate)", () => {
  // The load-bearing case for lastBottomScrollHeight, and the one the
  // "clamps always land at dist 0" argument misses: the clamp does land at
  // dist 0, but that instant is never observed. onScroll reads the LIVE
  // DOM at dispatch time, so a chunk landing in between makes the clamp
  // observable only at dist != 0. Neither at-bottom check can rescue it —
  // only the old-bottom witness can. Deleting the branch fails this test
  // while the rest of the suite stays green, so it is the regression gate
  // for the whole #11/#1238/#1297/#1431 family.

  it("composer shrink + message append + chunk-before-dispatch → stays sticky", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    ctl.notifyPinnedToBottom({ scrollTop: 1500, scrollHeight: 2000, clientHeight: 500 });
    // send: composer 500→600 and the message appends (+21) → max scrollTop
    // 1421, so the browser clamps 1500 → 1421. A chunk then grows
    // scrollHeight to 2500 BEFORE the scroll event dispatches:
    //   dist vs new bottom   = 2500 - 1421 - 600 = 479  (outside 120)
    //   prevScrollTop - curr = 1500 - 1421 = 79         (> unstickDeltaPx 20)
    //   dist vs last bottom  = 2000 - 1421 - 600 = -21  (inside → never moved)
    ctl.handleScroll({ scrollTop: 1421, scrollHeight: 2500, clientHeight: 600 });
    expect(ctl.isSticky()).toBe(true);
  });

  it("the same shape, but the user really did scroll up → unsticks", () => {
    // Same clamp + growth, except the user also scrolled up 300px. Now the
    // old-bottom witness disagrees too (2000 - 1121 - 600 = 279, outside),
    // so the deliberate gesture survives the rescue branch.
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    ctl.notifyPinnedToBottom({ scrollTop: 1500, scrollHeight: 2000, clientHeight: 500 });
    ctl.handleScroll({ scrollTop: 1121, scrollHeight: 2500, clientHeight: 600 });
    expect(ctl.isSticky()).toBe(false);
  });
});

describe("browser scrollTop clamps observed at the bottom", () => {
  it("composer shrink on send (clientHeight grows → clamp) keeps sticky", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 2000, 500); // bottom @ clientHeight 500 → scrollTop 1500
    // send: composer shrinks, viewport 500 → 600; browser clamps 1500 → 1400.
    // Nothing grew before dispatch, so the clamp is observed at dist 0.
    ctl.handleScroll(view(1400, 2000, 600));
    expect(ctl.isSticky()).toBe(true);
  });

  it("partial→final content swap (scrollHeight shrinks → clamp) keeps sticky", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 2000); // scrollTop 1400
    // final content is 300px shorter; browser clamps 1400 → 1100 (dist 0)
    ctl.handleScroll(view(1100, 1700));
    expect(ctl.isSticky()).toBe(true);
  });

  it("an unstuck reader is not pinned over by a layout change above them", () => {
    // Reader at 900 of a 2000px timeline; a collapse shrinks content to
    // 1800 but they are still 300px from the bottom → keep them alone.
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 2000);
    ctl.handleScroll(view(900, 2000));
    expect(ctl.isSticky()).toBe(false);
    expect(ctl.handleLayoutChange(view(900, 1800))).toBe(false);
  });
});

describe("layout changes with no scroll event (handleLayoutChange)", () => {
  // A height change moves the bottom without firing a scroll event, so it
  // is the one way the flag and the real position can drift apart. The
  // controller applies the same at-bottom rule here — one owner, one rule.

  it("collapse-all leaves an unstuck reader at the bottom → re-stick and pin", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 3000);
    ctl.handleScroll(view(1500, 3000)); // reading history → unstuck
    expect(ctl.isSticky()).toBe(false);
    // content-toggle collapse-all shrinks 3000 → 2050; the reader at 1500
    // is now dist = 2050-1500-600 = -50 → past the bottom.
    expect(ctl.handleLayoutChange(view(1500, 2050))).toBe(true);
    expect(ctl.isSticky()).toBe(true); // flag agrees with the button's measure
  });

  it("mobile keyboard dismissed (clientHeight grows) leaves the reader at the bottom → re-stick", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 2000, 400);
    ctl.handleScroll(view(1000, 2000, 400)); // unstuck (dist 600)
    expect(ctl.isSticky()).toBe(false);
    // keyboard hides: clientHeight 400 → 900, dist = 2000-1000-900 = 100 < 180
    expect(ctl.handleLayoutChange(view(1000, 2000, 900))).toBe(true);
  });

  it("growth while sticky pins; growth while unstuck does not", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 1000);
    expect(ctl.handleLayoutChange(view(400, 1400))).toBe(true); // still following
    const other = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(other, 3000);
    other.handleScroll(view(1000, 3000)); // unstuck
    expect(other.handleLayoutChange(view(1000, 3400))).toBe(false);
  });
});

describe("iOS bounce & touch (position-based, no wheel events)", () => {
  it("overscroll bounce at the bottom (scrollTop drops tens of px) stays sticky", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 1000); // scrollTop 400
    // bounce: 400 → 350 → 380 → 400; all within the 120px touch zone
    ctl.handleScroll(view(350, 1000));
    expect(ctl.isSticky()).toBe(true);
    ctl.handleScroll(view(380, 1000));
    ctl.handleScroll(view(400, 1000));
    expect(ctl.isSticky()).toBe(true);
  });

  it("a real touch flick (fast, beyond the zone) unsticks", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 2000); // scrollTop 1400
    ctl.handleScroll(view(1100, 2000)); // drop 300 > 20, dist 300 >= 120
    expect(ctl.isSticky()).toBe(false);
  });

  it("manual swipe back to the bottom re-sticks (#288)", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 2000);
    ctl.handleScroll(view(1100, 2000)); // unstick
    ctl.handleScroll(view(1350, 2000)); // dist 50 < 120 → re-stick
    expect(ctl.isSticky()).toBe(true);
  });
});

describe("wheel intent (mouse/trackpad)", () => {
  it("resting-finger twitch at the bottom is absorbed (#1048/#1235)", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1000); // scrollTop 400, dist 0
    ctl.handleWheel(-5, view(400, 1000));
    expect(ctl.isSticky()).toBe(true);
  });

  it("sub-noise deltas (|deltaY| ≤ 2) are ignored everywhere", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 2000);
    ctl.handleWheel(-2, view(1000, 2000)); // far from bottom but just noise
    expect(ctl.isSticky()).toBe(true);
  });

  it("an upward notch outside the zone unsticks (slow scroll-up escape, #1027)", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 2000); // scrollTop 1400
    // the user has crept up 40px (beyond the 30px pointer zone) via tiny
    // scroll deltas that never individually exceeded unstickDeltaPx…
    ctl.handleScroll(view(1385, 2000)); // drop 15 ≤ 20 → still sticky
    ctl.handleScroll(view(1370, 2000)); // drop 15 → still sticky
    ctl.handleScroll(view(1360, 2000)); // dist 40 ≥ 30, still sticky (small deltas)
    expect(ctl.isSticky()).toBe(true);
    // …then the next wheel notch expresses the intent position deltas can't:
    ctl.handleWheel(-10, view(1360, 2000));
    expect(ctl.isSticky()).toBe(false);
  });

  it("twitch at the bottom right after a button click (sticky, no anchor yet) is absorbed", () => {
    // requestStick deliberately leaves no bottom anchor, so the
    // old-bottom guard cannot fire here — the at-bottom guard is the only
    // thing standing between a resting-finger twitch and a broken ride.
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 3000);
    ctl.handleScroll(view(800, 3000)); // unstuck → anchor dropped
    ctl.requestStick(); // button: sticky on, still no anchor
    ctl.handleWheel(-5, view(2400, 3000)); // ride reached the bottom; twitch
    expect(ctl.isSticky()).toBe(true);
  });

  it("downward wheel does not unstick and does not force re-stick", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 2000);
    ctl.handleScroll(view(1000, 2000)); // unstick
    ctl.handleWheel(50, view(1000, 2000)); // wheel down far from bottom
    expect(ctl.isSticky()).toBe(false);
  });
});

describe("streaming growth race (content grows between events)", () => {
  // A chunk can land between a wheel/scroll event and the pin that will
  // follow it: measured against the NEW scrollHeight the user looks far
  // from the bottom, yet they never moved. lastBottomScrollHeight (from
  // the last pin) is the witness.

  it("wheel-up measured against freshly-grown content does not unstick a bottom-follower", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1000); // scrollTop 400, lastBottom = 1000
    // chunk grows content 1000 → 1200 (no scroll event yet); a trackpad
    // twitch fires: dist vs new = 1200-400-600 = 200 (outside 30px zone),
    // dist vs last-pinned bottom = 1000-400-600 = 0 → absorbed.
    ctl.handleWheel(-5, view(400, 1200));
    expect(ctl.isSticky()).toBe(true);
  });

  it("scroll event against freshly-grown content keeps a bottom-follower sticky", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1000); // scrollTop 400, lastBottom = 1000
    // Growth to 1200 plus a 5px jitter: the event lands at 395, which is
    // 5px off the expected echo (400) — so it is judged as user input.
    // dist vs new bottom = 205 (outside the 30px zone), dist vs the
    // last-pinned bottom = 5 (inside) → the user never moved, stay sticky.
    ctl.handleScroll(view(395, 1200));
    expect(ctl.isSticky()).toBe(true);
  });

  it("a manual scroll back to the bottom re-arms the witness for the next race", () => {
    // The anchor is dropped on unstick, so returning to the bottom by hand
    // (scrollbar / touch / keyboard — no pin involved) must re-arm it;
    // otherwise the very next growth race breaks sticky again.
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1000);
    ctl.handleScroll(view(200, 1000)); // deliberate scroll-up → unstuck, anchor dropped
    expect(ctl.isSticky()).toBe(false);
    ctl.handleScroll(view(400, 1000)); // manual scroll back to the bottom → re-stick
    expect(ctl.isSticky()).toBe(true);
    // streaming resumes: chunk grows to 1200 and the user nudges 25px —
    // beyond unstickDeltaPx, but still inside the OLD bottom zone, so only
    // a re-armed witness can tell "they never really left".
    ctl.handleScroll(view(375, 1200));
    expect(ctl.isSticky()).toBe(true);
  });

  it("a genuine scroll-up past the old bottom zone still unsticks during growth", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1000); // scrollTop 400
    // user wheels up 100px while a chunk lands: outside the zone under
    // BOTH measurements (new dist 300, old dist 100 ≥ 30) → unstick.
    ctl.handleWheel(-100, view(300, 1200));
    expect(ctl.isSticky()).toBe(false);
  });

  it("old-bottom acceptance never force-re-sticks a deliberately-unstuck user (#1320)", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1000); // lastBottom = 1000
    ctl.handleScroll(view(200, 1000)); // deliberate scroll-up → unstuck
    expect(ctl.isSticky()).toBe(false);
    // content grows; a jitter event happens to land them near the OLD
    // bottom position (e.g. 380): dist vs old = 1000-380-600 = 20 < 30,
    // but they were unstuck — the old-bottom branch must not re-stick.
    ctl.handleScroll(view(380, 1200));
    expect(ctl.isSticky()).toBe(false);
  });

  it("before any pin, the old-bottom branch cannot fire (null anchor)", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    // no pin yet; a long timeline mounted and the user scrolls up fast
    ctl.handleScroll(view(1000, 2000)); // baseline event (prevScrollTop was 0 → downward)
    ctl.handleScroll(view(500, 2000)); // drop 500 → must unstick
    expect(ctl.isSticky()).toBe(false);
  });
});

describe("force-stick requests (send / agent switch / button)", () => {
  it("pin after agent switch re-sticks regardless of prior state (#1391)", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 2000);
    ctl.handleScroll(view(500, 2000)); // user reading history
    expect(ctl.isSticky()).toBe(false);
    pin(ctl, 3000); // switch → effect pins the new thread
    expect(ctl.isSticky()).toBe(true);
    ctl.handleScroll(view(2400, 3000)); // the echo
    expect(ctl.isSticky()).toBe(true);
  });

  it("requestStick survives the button's smooth downward ride", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 3000);
    ctl.handleScroll(view(800, 3000)); // unstuck, far up
    expect(ctl.isSticky()).toBe(false);
    ctl.requestStick(); // button click → smooth scrollTo(bottom)
    expect(ctl.isSticky()).toBe(true);
    // smooth scroll emits many intermediate DOWNWARD events, none of
    // which match an expected value — they must not unstick:
    ctl.handleScroll(view(1200, 3000));
    ctl.handleScroll(view(1800, 3000));
    expect(ctl.isSticky()).toBe(true);
    ctl.handleScroll(view(2400, 3000)); // arrival, dist 0
    expect(ctl.isSticky()).toBe(true);
  });

  it("growth during the smooth ride pins immediately (already sticky)", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 3000);
    ctl.handleScroll(view(800, 3000));
    ctl.requestStick();
    expect(ctl.handleLayoutChange(view(1200, 3400))).toBe(true); // RO pins on the growth
  });
});

describe("agent-switch background refetch (#409)", () => {
  it("spurious wheel AFTER the refetch grew the content does not kill the follow-up pin", () => {
    // #409's actual shape: the twitch fires once the refetch has already
    // inflated scrollHeight, so isAtBottom says "far from the bottom" and
    // only the old-bottom witness knows the user never moved. (Measuring
    // the twitch before the growth would be absorbed by isAtBottom and
    // would not exercise this at all.)
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1000); // switch → pin on the restored (cached) timeline; scrollTop 400
    // refetch lands: scrollHeight 1000 → 1600, no scroll event yet.
    // dist vs new = 1600-400-600 = 600 (outside 30); vs last bottom = 0.
    ctl.handleWheel(-3, view(400, 1600));
    expect(ctl.isSticky()).toBe(true);
    expect(ctl.handleLayoutChange(view(400, 1600))).toBe(true);
  });

  it("a deliberate wheel-up after the refetch still unsticks", () => {
    // Same growth, but the user has moved 100px above the old bottom →
    // outside the zone under both measurements → the gesture wins.
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1000);
    ctl.handleWheel(-60, view(300, 1600));
    expect(ctl.isSticky()).toBe(false);
  });
});

describe("stale bottom anchor cannot project a phantom zone (codex finding 2)", () => {
  // lastBottomScrollHeight is dropped on unstick. Retained, it would go
  // stale as content grows and mark a band in the MIDDLE of a long
  // timeline as "the bottom", swallowing the user's scroll-up gestures
  // there — including the one aborting the button's smooth ride.

  it("aborting the button's smooth ride with a wheel-up unsticks", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 3000); // last bottom = 3000
    ctl.handleScroll(view(800, 3000)); // read history → unstick (anchor dropped)
    // streaming grows the timeline well past the old bottom
    expect(ctl.handleLayoutChange(view(800, 5000))).toBe(false);
    ctl.requestStick(); // button click → smooth ride begins
    expect(ctl.isSticky()).toBe(true);
    // mid-ride the user changes their mind at scrollTop 2380 — a point
    // that the STALE anchor (3000 - 2380 - 600 = 20 < 30) would have
    // called "the bottom" and swallowed.
    ctl.handleWheel(-80, view(2380, 5000));
    expect(ctl.isSticky()).toBe(false);
  });

  it("requestStick does not resurrect an anchor: growth mid-ride cannot fake old-bottom", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 3000);
    ctl.handleScroll(view(800, 3000)); // unstuck
    ctl.requestStick();
    // A scroll-up at the stale old-bottom band must be judged on position
    // alone, not rescued by the dropped anchor.
    ctl.handleScroll(view(2380, 5000)); // downward from 800 → keeps sticky
    ctl.handleScroll(view(2300, 5000)); // 80px up → unstick
    expect(ctl.isSticky()).toBe(false);
  });
});

describe("wheel-up during streaming still escapes (codex finding 1)", () => {
  // handleWheel absorbs an upward notch while the user is at the last
  // known bottom (it cannot yet tell a twitch from a gesture — the wheel
  // has not moved the viewport). The scroll event that follows carries the
  // actual movement and unsticks. Per the HTML update-the-rendering steps,
  // scroll events dispatch BEFORE ResizeObserver callbacks, so the unstick
  // lands before any pin could yank the user back.

  it("wheel absorbed at the old bottom, then its scroll event unsticks", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1000); // scrollTop 400, last bottom = 1000
    // chunk grows to 1200; user wheels up 80 before the RO pins.
    ctl.handleWheel(-80, view(400, 1200)); // absorbed (still at old bottom)
    expect(ctl.isSticky()).toBe(true);
    // the wheel's scroll lands: 400 → 320.
    //   dist vs new = 1200-320-600 = 280 (outside 30)
    //   dist vs old = 1000-320-600 =  80 (outside 30) → no rescue
    //   prev - curr = 400-320      =  80 (> 20)       → unstick
    ctl.handleScroll(view(320, 1200));
    expect(ctl.isSticky()).toBe(false);
    // and the RO, running after the scroll steps, must not pin them back
    expect(ctl.handleLayoutChange(view(320, 1200))).toBe(false);
  });
});


describe("anchor freshness (stale lastBottomScrollHeight after content shrink)", () => {
  it("nearLastKnownBottom branch keeps the anchor stable across growth (#564)", () => {
    // #564 regression gate: the branch must NOT refresh the anchor to the
    // grown scrollHeight. A refreshed anchor makes the NEXT event measure
    // the viewport against the new bottom — a bottom-follower who never
    // moved (still at the OLD bottom) then looks far from it and falsely
    // unsticks. This is the switch race: pin on the cached (shorter)
    // thread, growth before the clamp echo, echo refreshes the anchor,
    // next wheel/scroll kills sticky permanently.
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1000); // scrollTop 400, anchor 1000
    ctl.handleScroll(view(400, 1100)); // growth echo, near old bottom
    expect(ctl.isSticky()).toBe(true);
    // The wheel that follows must still be absorbed — a refreshed anchor
    // (1100) would measure 1100-400-600=100 > zone and unstick.
    ctl.handleWheel(-25, view(400, 1200));
    expect(ctl.isSticky()).toBe(true);
  });

  it("content shrink in scroll event updates anchor instead of unstick", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1100);
    // Content shrunk then grew: scrollHeight=1050 < lastBottom=1100
    ctl.handleScroll(view(400, 1050));
    // shrink branch: 1050 < 1100 -> true -> keep sticky, update anchor
    expect(ctl.isSticky()).toBe(true);
  });

  it("user scroll-up while content grew still unsticks", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1000);
    // Content grew: scrollHeight(1200) > lastBottom(1000)
    ctl.handleScroll(view(320, 1200));
    // shrink branch skipped (1200 > 1000), unstick fires (80 > 20)
    expect(ctl.isSticky()).toBe(false);
  });

  it("handleLayoutChange updates anchor when sticky even if not at bottom", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1100);
    ctl.handleLayoutChange(view(400, 1050));
    // isAtBottom false, sticky true -> anchor updated to 1050
    expect(ctl.isSticky()).toBe(true);
    pin(ctl, 1050);
    ctl.handleScroll(view(450, 1050));
    expect(ctl.isSticky()).toBe(true);
  });

  it("race: scroll event fires before RO with shrunk-then-grown content", () => {
    const ctl = createStickyController(POINTER_STICKY_THRESHOLDS);
    pin(ctl, 1100);
    // Scroll event fires before RO:
    ctl.handleScroll(view(400, 1050));
    // shrink branch: 1050 < 1100 -> keep sticky
    expect(ctl.isSticky()).toBe(true);
    // RO fires after:
    const shouldPin = ctl.handleLayoutChange(view(400, 1050));
    expect(shouldPin).toBe(true);
    expect(ctl.isSticky()).toBe(true);
  });
});

// #1016 — a touch drag is the user's own scroll position. While a drag is
// in progress, layout-change pins must not fight it: on touch devices a slow
// scroll-up during streaming was canceled every frame by the RO pin (the
// position rule measured against the just-pinned bottom, so the drag could
// never accumulate past the bottom zone) — the timeline stayed glued to the
// bottom and the scroll-to-bottom button never appeared. Wheel devices
// escape via handleWheel; touch devices have no wheel, so the gesture
// lifecycle is the intent channel.
describe("touch drag (handleTouchStart / handleTouchEnd)", () => {
  it("a layout change during an active touch drag must not pin — even at the bottom with growth", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 1000); // at bottom, sticky
    ctl.handleTouchStart();
    // A chunk lands mid-drag (scrollHeight grows; the user has not moved —
    // the drag just started). The old code pinned here every frame, which
    // is what canceled the user's scroll-up.
    expect(ctl.handleLayoutChange(view(400, 1200))).toBe(false);
    expect(ctl.isSticky()).toBe(true); // following is paused, not lost
  });

  it("release inside the bottom zone keeps following (iOS bounce tolerance)", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 1000);
    ctl.handleTouchStart();
    // The user drags up 50px — still inside the 120px zone (0.2 * 600).
    ctl.handleScroll(view(350, 1000));
    ctl.handleTouchEnd(view(350, 1000));
    expect(ctl.isSticky()).toBe(true);
    // A chunk after release still pins — they are still "at the bottom".
    expect(ctl.handleLayoutChange(view(350, 1100))).toBe(true);
  });

  it("release beyond the bottom zone stops following — a later chunk must not yank the reader back", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 1000);
    ctl.handleTouchStart();
    // Slow drag: each scroll event is inside the zone (the #1016 shape),
    // so handleScroll alone never unsticks.
    ctl.handleScroll(view(380, 1000));
    ctl.handleScroll(view(360, 1000));
    ctl.handleScroll(view(340, 1000));
    expect(ctl.isSticky()).toBe(true);
    // The finger lifts beyond the zone (dist = 1000 - 300 - 600 = 100 <
    // 120 → hmm, still inside — drag a bit further).
    ctl.handleScroll(view(250, 1000)); // dist = 150 >= 120 → outside
    ctl.handleTouchEnd(view(250, 1000));
    expect(ctl.isSticky()).toBe(false);
    // Streaming continues — no pin (the reader is gone from the bottom).
    expect(ctl.handleLayoutChange(view(250, 1200))).toBe(false);
    expect(ctl.handleLayoutChange(view(250, 1400))).toBe(false);
  });

  it("a chunk landing mid-drag pushes the user out of the zone — the touchEnd re-evaluation unsticks them", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 1000); // bottom: scrollTop 400, dist 0, sticky
    ctl.handleTouchStart();
    // 30px drag up — still inside the 120px zone, so the position rule
    // alone keeps following (this is the #1016 shape: per-event movement
    // never escapes the zone while the baseline is fresh).
    ctl.handleScroll(view(370, 1000));
    expect(ctl.isSticky()).toBe(true);
    // A chunk lands mid-drag. With the fix it does NOT pin (the user is
    // touching), but it does push the bottom away: the user's position is
    // now beyond the zone (dist = 1150 - 370 - 600 = 180) while the
    // position rule never fired (no scroll event accompanied the growth).
    ctl.handleLayoutChange(view(370, 1150));
    expect(ctl.isSticky()).toBe(true); // no pin, no state change — yet
    // The finger lifts beyond the zone: stop following, so the next chunk
    // cannot yank the reader back to the bottom.
    ctl.handleTouchEnd(view(370, 1150));
    expect(ctl.isSticky()).toBe(false);
    expect(ctl.handleLayoutChange(view(370, 1300))).toBe(false);
  });

  it("touchEnd applies the position rule even without a touchStart (defensive — stray events are harmless)", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 1000);
    // At the bottom: a stray touchend changes nothing.
    ctl.handleTouchEnd(view(400, 1000));
    expect(ctl.isSticky()).toBe(true);
    // Away from the bottom (content grew under a bottom-follower): the
    // position rule would unstick on the next scroll anyway — a stray
    // touchend just applies it early. Safe, never harmful.
    ctl.handleTouchEnd(view(100, 2000));
    expect(ctl.isSticky()).toBe(false);
  });

  it("a touch that ends back at the bottom re-sticks (scrolling back down)", () => {
    const ctl = createStickyController(TOUCH_STICKY_THRESHOLDS);
    pin(ctl, 1000);
    ctl.handleTouchStart();
    ctl.handleScroll(view(250, 1000)); // dragged away
    ctl.handleTouchEnd(view(250, 1000));
    expect(ctl.isSticky()).toBe(false);
    // Drags back down to the bottom and releases.
    ctl.handleTouchStart();
    ctl.handleScroll(view(400, 1000));
    ctl.handleTouchEnd(view(400, 1000));
    expect(ctl.isSticky()).toBe(true);
  });
});
