# Sticky-bottom scroll: single-writer controller replaces the patch-chain state machine

## Context

The timeline's sticky-bottom auto-scroll broke repeatedly — user sends a
message, agent streams code, iOS bounces, trackpad jitters — and each break
got its own fix. 13+ commits (#11, #288, #409, #415, #1027, #1048, #1069,
#1226, #1238, #1297, #1320, #1329, #1391, #1431, …) all patched the same
structure: a `stickyRef` written directly by six code paths (onScroll,
onWheel, ResizeObserver, two force-scroll effects, the scroll-to-bottom
button), each guessing what its DOM event meant. Five of them were fixes to
earlier fixes (#1320 fixed #1069's safety net; #1297 fixed #1238's reset;
the grace timer fixed #1235's guard).

Because DOM events arrive in unpredictable order relative to content
changes (a streamed chunk can land between a wheel event and its scroll
event; a programmatic scrollTop write dispatches a synchronous scroll event
in some browsers), every path × timing pair needed a compensating mechanism
— wheel-direction refs, prev-snapshot double resyncs, a 200ms grace timer,
double sticky assertions, layered at-bottom/old-bottom safety nets — and
each mechanism became a new racy writer.

## Decision

Make the sticky flag single-owner: `lib/sticky.ts:createStickyController`
(pure closure, DOM-free, vitest-drivable). The component only feeds events
in and performs the scrolls the controller requests. Two ideas carry it:

1. **The baseline is synced by whoever moves the viewport.** Every
   programmatic scroll-to-bottom reports itself (`notifyPinnedToBottom`)
   and moves `prevScrollTop` to the position it wrote. The scroll event
   echoing back therefore shows zero user movement and decides nothing. No
   timing flag, no direction heuristic, no grace window is needed to
   recognize "that scroll was us" — which is what the wheel-direction
   resets, the 200ms grace timer, and the double sticky assertions were all
   approximating.
2. **`lastBottomScrollHeight` is the witness for "the content grew under
   me"**: scrollHeight at the last moment the viewport was known to be at
   the bottom. Dropped on unstick, so a stale anchor can never project a
   phantom bottom zone into the middle of a long timeline.

Everything else is a request: send / agent switch / button →
`requestStick()` / pin; the ResizeObserver reports layout changes via
`handleLayoutChange(view)` and performs the pin it asks for.

### The load-bearing correction

An earlier draft of this decision justified deleting #1431's
clientHeight-growth discount with "browser clamps always land at distance 0
from the bottom, so the at-bottom rule absorbs them." **That reasoning is
wrong**, and review caught it. The clamp does land at distance 0 — but that
instant is never observed. `onScroll` reads the live DOM at dispatch time,
so a chunk landing in between makes the clamp observable only at distance
≠ 0, with neither at-bottom check able to rescue it.

What actually subsumes the discount is the old-bottom witness (2), not the
at-bottom rule. `sticky.test.ts` pins this case directly ("send flow: a
clamp OBSERVED after a chunk grew") — deleting the branch fails that test
while the rest of the suite stays green.

## Alternatives rejected

- **Keep patching the event-driven hysteresis** — rejected; the structure
  generates the bugs, and a third of the fixes were fixes to fixes.
- **Centralize into one reducer but keep delta/direction heuristics** —
  rejected; without a synced baseline the reducer still cannot tell user
  intent from echo, so the heuristics (and their races) survive
  centralization.
- **Timing flags (`isAutoScrolling` booleans / event suppression
  windows)** — rejected; the removed 200ms grace timer was exactly this,
  and it silently disabled deliberate unstick during heavy streaming
  (perpetual grace) while still missing multi-frame races.
- **An `expectedScrollTop` echo-matcher** (identify our own scroll by
  matching the event's position against the last programmatic write, ±1px)
  — implemented, then **cut before merge**: mutation testing showed the
  whole primitive could be deleted with the suite green, and no browser
  scenario could be constructed where it changed an outcome. Baseline sync
  (1) already makes the echo inert. Kept only as this note, because the
  first draft narrated it as the linchpin — an untested mechanism carrying
  a load-bearing story is worse than no mechanism.

## Consequences

- Wheel-intent unstick and the pointer/touch bottom-zone presets are
  preserved as-is (they encode real UX findings from #731/#1027/#415, not
  races).
- `handleWheel` absorbs an upward notch while the user sits at the last
  known bottom. That is safe rather than lossy: per the HTML
  update-the-rendering steps, scroll events dispatch before ResizeObserver
  callbacks, so the wheel's own scroll event unsticks before any pin can
  yank the user back. Pinned by test.
- Known parity limitation, accepted: during heavy streaming, a slow
  trackpad scroll-up whose per-frame movement stays inside the bottom zone
  still fights the auto-pin until a notch lands outside the zone —
  identical to prior behavior. A cumulative wheel-delta escape could
  improve it later; it would slot into `handleWheel` without touching the
  ownership model.
- Component-level scroll behavior remains untestable in jsdom, so
  `sticky.test.ts` is the only guard. It is therefore held to a mutation
  standard: every primitive in the controller has a test that fails when
  that primitive is removed. Anything that cannot be pinned that way gets
  deleted rather than shipped (see the `expectedScrollTop` rejection).
