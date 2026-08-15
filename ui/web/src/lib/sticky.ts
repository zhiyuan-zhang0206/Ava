// Sticky-bottom scroll controller — the SINGLE owner of the sticky flag.
//
// History: the previous design let six code paths (onScroll, onWheel,
// ResizeObserver, two force-scroll effects, the scroll-to-bottom button)
// write a shared stickyRef directly, each guessing what its event meant.
// DOM events arrive in unpredictable order relative to content changes
// (a streamed chunk can land between a wheel event and its scroll event;
// a programmatic scrollTop write dispatches a synchronous scroll event in
// some browsers), so every path × timing pair needed its own patch —
// wheel-direction refs, grace timers, double sticky assertions, prev-
// snapshot resyncs — and each patch became a new racy writer. 13+ fixes
// later the structure itself was the bug.
//
// This controller owns the flag outright. Two ideas carry the whole
// design:
//
// 1. **A pin moves the baseline with it.** notifyPinnedToBottom sets
//    `prevScrollTop` to the position it just wrote, so the scroll event
//    that echoes back shows zero user movement and changes nothing — no
//    timing flag, no direction heuristic, no grace window needed to
//    recognize "that scroll was us". This is what the old design's
//    wheel-direction resets, 200ms grace timer, and double sticky
//    assertions were all approximating.
//
// 2. **lastBottomScrollHeight is the witness for "the content grew under
//    me".** It records scrollHeight at the last moment the viewport was
//    known to be at the bottom (every pin, every at-bottom scroll).
//    Scroll events are read from the LIVE DOM at dispatch time, not from
//    the event, so a chunk landing between a scroll and its dispatch makes
//    the reader look far from the (new) bottom even though they never
//    moved — measured against the last known bottom, they are still there.
//    Cleared on unstick so a stale anchor can never resurrect a phantom
//    bottom zone in the middle of a long timeline.
//
// Note (2) is what actually protects the send flow, NOT the at-bottom
// rule: a browser clamp does land exactly at distance 0, but that instant
// is never observed — by the time the scroll event dispatches, a chunk may
// have grown scrollHeight, so the clamp is observed at distance != 0. The
// old clientHeight-growth discount existed for this case; the old-bottom
// witness is what subsumes it. (`sticky.test.ts` pins this directly — the
// suite is the only guard, since jsdom does not render scroll.)
//
// Everything else is a REQUEST into the controller: send / agent switch /
// button call requestStick(); the ResizeObserver reports the layout change
// via handleLayoutChange() and performs the pin it asks for. No caller
// mutates the flag.
//
// The bottom zone width is pointer-dependent (the component picks the
// preset from `(pointer: coarse)`):
//   - touch: bottomPxRatio * clientHeight clamped to [80, 200]. The wide
//     zone absorbs iOS WebKit momentum + overscroll bounce, which leaves
//     scrollTop 100-150px short of the bottom even when the user clearly
//     means "at the bottom".
//   - mouse / trackpad: a much tighter zone (no overscroll bounce exists),
//     so a small deliberate scroll-up to read a message escapes sticky
//     instead of being yanked back on the next streamed item.

export interface StickyThresholds {
  // A user scroll must move UP by more than this px (within one event) to
  // count as a deliberate scroll-up. Filters sub-pixel rebounds; real
  // scrolls are tens of pixels per event. (Slow multi-event scroll-ups on
  // wheel devices are caught by the wheel-intent path instead.)
  unstickDeltaPx: number;
  // "At the bottom" zone = bottomPxRatio * clientHeight, clamped to
  // [bottomPxMin, bottomPxMax].
  bottomPxRatio: number;
  bottomPxMin: number;
  bottomPxMax: number;
}

// Touch devices: wide bounce-tolerant zone.
export const TOUCH_STICKY_THRESHOLDS: StickyThresholds = {
  unstickDeltaPx: 20,
  bottomPxRatio: 0.2,
  bottomPxMin: 80,
  bottomPxMax: 200,
};

// Mouse / trackpad: tight zone. No overscroll bounce to absorb, so a
// deliberate scroll-up of one wheel notch leaves the bottom zone and
// unsticks — the timeline stops yanking the user back while they read.
export const POINTER_STICKY_THRESHOLDS: StickyThresholds = {
  unstickDeltaPx: 20,
  bottomPxRatio: 0.05,
  bottomPxMin: 24,
  bottomPxMax: 72,
};

// Default = touch preset, preserving prior behavior for callers that do
// not pass thresholds.
const DEFAULT_STICKY_THRESHOLDS = TOUCH_STICKY_THRESHOLDS;

export interface ScrollSnapshot {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

// Effective "at the bottom" zone in px: bottomPxRatio * clientHeight,
// clamped to [bottomPxMin, bottomPxMax]. Shared by the sticky controller
// (stick when entering the zone) and the scroll-to-bottom button (show
// when outside the zone).
export function bottomZone(
  clientHeight: number,
  thresholds: StickyThresholds = DEFAULT_STICKY_THRESHOLDS,
): number {
  return Math.min(
    thresholds.bottomPxMax,
    Math.max(thresholds.bottomPxMin, clientHeight * thresholds.bottomPxRatio),
  );
}

// Whether the viewport is currently within bottomZone of the bottom.
// A pure function of the *current* position — recomputed from a fresh
// measurement, so it stays correct across layout-height changes that
// fire no scroll event (async image/markdown load, content-toggle,
// viewport resize). Drives scroll-to-bottom button visibility and the
// controller's position rule.
export function isAtBottom(
  curr: ScrollSnapshot,
  thresholds: StickyThresholds = DEFAULT_STICKY_THRESHOLDS,
): boolean {
  const dist = curr.scrollHeight - curr.scrollTop - curr.clientHeight;
  return dist < bottomZone(curr.clientHeight, thresholds);
}

// Wheel events whose |deltaY| is at or below this are sub-pixel noise
// (trackpad resting-finger jitter); a deliberate notch easily exceeds it.
const WHEEL_NOISE_PX = 2;

export interface StickyController {
  /** Current sticky flag. True = content growth pins the viewport to the
   * bottom. */
  isSticky(): boolean;
  /** Feed every viewport scroll event here (snapshot read from the live
   * DOM, not the event). */
  handleScroll(view: ScrollSnapshot): void;
  /** Feed every wheel event here. An upward notch outside the bottom zone
   * (measured against both the current and last-known bottom) is the
   * deliberate "stop following" gesture — the only unstick signal a slow
   * scroll-up can produce, since per-event scroll deltas stay under
   * unstickDeltaPx while auto-scroll keeps re-pinning the baseline. */
  handleWheel(deltaY: number, view: ScrollSnapshot): void;
  /** Report a layout change (ResizeObserver) and ask whether to pin.
   * Takes a snapshot because a height change moves the bottom with no
   * scroll event: collapsing content or a growing viewport (mobile
   * keyboard dismissed) can leave the viewport visually at the bottom
   * without any event to reconcile the flag. Applying the same at-bottom
   * rule here keeps the flag honest without adding a second writer. */
  handleLayoutChange(view: ScrollSnapshot): boolean;
  /** A touch drag started (touchstart). While a drag is in progress the
   * user's finger IS the scroll position — layout-change pins must not
   * fight it (#1016: on touch devices a slow scroll-up during streaming
   * was canceled every frame by the RO pin, so the timeline stayed glued
   * to the bottom and the scroll-to-bottom button never appeared; wheel
   * devices escape via handleWheel, touch devices have no wheel). */
  handleTouchStart(): void;
  /** The touch drag ended (touchend / touchcancel). Re-evaluate the
   * position rule: released beyond the bottom zone → stop following, so a
   * later chunk cannot yank the reader back. Released inside the zone
   * (iOS bounce tolerance) → keep following. */
  handleTouchEnd(view: ScrollSnapshot): void;
  /** Force sticky on WITHOUT scrolling — for the button's smooth
   * scrollTo, where the ride to the bottom is asynchronous. The position
   * rule keeps sticky through the downward ride. */
  requestStick(): void;
  /** Report a performed pin-to-bottom (viewport.scrollTop was set to
   * scrollHeight). Pass the post-write snapshot so the browser-clamped
   * actual position becomes the new baseline. Sets sticky — a programmatic
   * scroll-to-bottom always re-enables following. */
  notifyPinnedToBottom(view: ScrollSnapshot): void;
  // There is deliberately no notify() for non-pin programmatic scrolls
  // (prepend compensation). They need none: the only reader of the
  // baseline is handleScroll, which re-syncs it on every event, so the
  // echo heals the baseline itself. A stale baseline can only understate
  // upward movement — it biases toward staying sticky, never toward a
  // false unstick.
}

export function createStickyController(
  thresholds: StickyThresholds = DEFAULT_STICKY_THRESHOLDS,
): StickyController {
  // Starts sticky: a fresh timeline pins to the bottom on mount.
  let sticky = true;
  // scrollHeight when the viewport was last known to be at the bottom;
  // null when not following (see unstick). Witness for the growth race.
  let lastBottomScrollHeight: number | null = null;
  // Last known scrollTop — baseline for the moved-up check. Written by
  // every scroll event, and moved ahead by a pin so the pin's own echo
  // reads as zero user movement.
  let prevScrollTop = 0;
  // A touch drag is in progress (touchstart seen, no touchend yet). While
  // true the user's finger owns the scroll position: handleLayoutChange
  // must not ask for a pin (#1016 — see the interface docs).
  let touchActive = false;

  // Following: remember where the bottom was, so a later growth can be
  // told apart from a user scrolling away.
  const stick = (view: ScrollSnapshot) => {
    sticky = true;
    lastBottomScrollHeight = view.scrollHeight;
  };

  // Not following: drop the bottom anchor. A retained anchor would go
  // stale as the content grows and project a phantom bottom zone into the
  // middle of the timeline, where it would swallow the user's later
  // scroll-up gestures (including the one aborting the button's smooth
  // ride back to the bottom).
  const unstick = () => {
    sticky = false;
    lastBottomScrollHeight = null;
  };

  const nearLastKnownBottom = (view: ScrollSnapshot): boolean =>
    lastBottomScrollHeight !== null &&
    lastBottomScrollHeight - view.scrollTop - view.clientHeight <
      bottomZone(view.clientHeight, thresholds);

  return {
    isSticky: () => sticky,

    handleScroll(view) {
      if (isAtBottom(view, thresholds)) {
        // At the bottom → follow, whatever moved us here. The wide touch
        // zone absorbs iOS overscroll bounce / address-bar jitter. This is
        // also the manual re-stick path (scrollbar / touch / keyboard back
        // to the bottom).
        stick(view);
      } else if (sticky && nearLastKnownBottom(view)) {
        // The content grew under a bottom-follower between the scroll and
        // its dispatch — they never moved, keep following. Only
        // *preserves* stick: an already-unstuck user has no anchor, so
        // this branch cannot pull them back.
        //
        // Deliberately does NOT refresh the anchor here. Refreshing it to
        // the grown scrollHeight would make the very next scroll/wheel
        // event measure the viewport against the NEW bottom — a
        // bottom-follower who never moved (still at the OLD bottom) would
        // then look far from it and falsely unstick. That is the #564
        // race: an agent switch pins the (shorter) cached thread, SSE/HTTP
        // growth lands before the clamp echo dispatches, the echo
        // refreshes the anchor, and the next event (trackpad momentum /
        // wheel) reads "far from bottom" and permanently kills sticky
        // (the RO cannot re-pin an unstuck viewport). Keeping the anchor
        // at the last TRUE bottom absorbs every event until the RO pin
        // (which is what legitimately advances it). #483's shrink branch
        // below still handles content SHRINK — the only case where the
        // anchor must move without a pin.
      } else if (sticky && lastBottomScrollHeight !== null &&
                 view.scrollHeight < lastBottomScrollHeight) {
        // Content shrunk since the last pin (e.g. async syntax
        // highlighting collapsed a code block). The browser likely
        // clamped scrollTop in response, producing an apparent scroll-up
        // that was not a user gesture. Update the anchor to the new
        // (smaller) height; do not unstick.
        lastBottomScrollHeight = view.scrollHeight;
      } else if (prevScrollTop - view.scrollTop > thresholds.unstickDeltaPx) {
        // Deliberate upward movement away from the bottom.
        unstick();
      }
      // else: downward or small motion away from the bottom — keep state.
      // Downward must not unstick: the button's smooth ride and a user
      // scrolling back toward the bottom both pass through here.
      prevScrollTop = view.scrollTop;
    },

    handleWheel(deltaY, view) {
      if (deltaY >= -WHEEL_NOISE_PX) return; // downward or noise
      // Upward intent. Absorb it while at the bottom (resting-finger
      // twitches during streaming must not break following) or while still
      // at the last known bottom (a chunk grew the content between this
      // wheel and the pin that follows — the user never moved). A genuine
      // scroll-up beyond the zone reaches the scroll handler anyway: the
      // browser dispatches scroll events before ResizeObserver callbacks
      // in the update-the-rendering steps, so the position rule unsticks
      // before any pin can yank the user back.
      if (isAtBottom(view, thresholds)) return;
      if (sticky && nearLastKnownBottom(view)) return;
      unstick();
    },

    handleLayoutChange(view) {
      // An active touch drag is the user's own scroll position — never
      // pin under their finger. The growth is not lost: the drag's own
      // scroll events keep feeding handleScroll, and the position rule
      // (plus the touchEnd re-evaluation below) reconcile sticky when the
      // finger lifts. #1016: without this, every streaming chunk pinned
      // the viewport back to the bottom mid-drag, so a slow scroll-up
      // could never accumulate past the bottom zone — the timeline was
      // glued to the bottom and the scroll-to-bottom button never showed.
      if (touchActive) return false;
      // A height change moved the bottom with no scroll event. If it left
      // the viewport at the bottom, that IS following — reconcile the flag
      // (the old design needed a separate at-bottom net in the observer
      // for exactly this, which then raced the other writers).
      if (isAtBottom(view, thresholds)) stick(view);
      // Keep the anchor current even when the viewport has drifted from
      // the bottom (content may shrink then grow again before this
      // observer callback, e.g. async syntax highlighting collapsing a
      // code block). Without this, lastBottomScrollHeight stays at the
      // pre-shrink height and nearLastKnownBottom computes against a
      // stale reference on the next scroll event.
      else if (sticky) lastBottomScrollHeight = view.scrollHeight;
      return sticky;
    },

    handleTouchStart() {
      touchActive = true;
    },

    handleTouchEnd(view) {
      touchActive = false;
      // The finger lifted. If the user is beyond the bottom zone, they
      // meant to leave the bottom — stop following so the next chunk does
      // not yank them back (the position rule needs a scroll event to
      // unstick, and a released finger produces none). Inside the zone
      // (iOS bounce tolerance / a tap) following continues.
      if (sticky && !isAtBottom(view, thresholds)) unstick();
    },

    requestStick() {
      // No snapshot: the caller is about to scroll asynchronously (smooth
      // ride). Deliberately does NOT restore a bottom anchor — the pin on
      // arrival sets one. Sticking without an anchor keeps the ride
      // abortable: an upward wheel mid-ride finds no phantom old bottom to
      // hide behind and unsticks.
      sticky = true;
    },

    notifyPinnedToBottom(view) {
      stick(view);
      prevScrollTop = view.scrollTop;
    },
  };
}
