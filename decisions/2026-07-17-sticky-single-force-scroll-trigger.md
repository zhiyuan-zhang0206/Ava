# Sticky-bottom: one store-owned force-scroll trigger (and why NOT event-driven)

Extends [2026-07-15-sticky-single-writer-controller.md](2026-07-15-sticky-single-writer-controller.md).
That decision made the sticky *flag* single-owner (`lib/sticky.ts`). This one
makes the *force-scroll trigger* single-owner. The controller and its
ResizeObserver-driven pinning are untouched.

## Context

After the single-writer controller shipped, agent switch still misbehaved
intermittently: switching to another agent sometimes did not land at the
bottom, or auto-scroll stopped following the new agent's stream. The task that
prompted this (`ava-1957`) proposed a full **event-driven rewrite** — drive the
scroll off SSE items ("new item arrives → if sticky, scroll") and delete the
ResizeObserver + witness machinery.

Investigation put the fault somewhere else. The controller was not the problem;
the **trigger layer** was. Two independent force-scroll paths fed it:

- `scrollToken` — a monotonic counter owned by `page.tsx`, bumped on **both**
  agent switch (a `useEffect` on `activeId`) and send.
- an `activeThreadId` `useLayoutEffect` in the timeline — a **second** pin on
  switch, reading the store directly.

On every switch both fired → two pins at different commits. Worse, the
`scrollToken` bump ran in `page.tsx` *before* the store had swapped in the new
thread's items, so the pin measured a stale bottom. This is exactly the
"two writers, each guessing" smell the controller work removed — resurfaced one
layer up.

## Decision

Collapse both into **one store-owned signal**: `scrollToBottomRequest`, a
monotonic counter in the timeline slice.

- Bumped in the SAME `set()` as the new thread's items — inside
  `restoreTimeline` (hot cache) and `resetThread` (cold cache) — and via
  `requestScrollToBottom()` on send.
- NOT bumped by `reloadSnapshot` (mid-stream refresh) or `prependOlder`
  (scroll-up load-older): either would yank a reading user to the bottom.
- Consumed by ONE `useLayoutEffect` in the timeline → `pinToBottom` (which sets
  sticky via `notifyPinnedToBottom`).

`page.tsx` no longer owns a `scrollToken`; the `activeThreadId` pin effect is
gone. One signal, one owner, one consumer.

Bumping in the items `set()` is what fixes the race: on a hot-cache switch the
layout effect now runs with the full new thread already in the DOM and pins its
true bottom. On a cold-cache switch it pins the empty viewport; items arrive via
React Query and the **latched sticky flag + ResizeObserver** pull the viewport
down as they lay out — no second trigger required.

## Alternative rejected: the event-driven rewrite

Driving auto-scroll off SSE items and deleting the ResizeObserver was rejected
as **architecturally backwards** — it would regress behavior, not simplify:

- The ResizeObserver fires pre-paint on ANY height change: streamed chunks, the
  throttled markdown/highlight flush, async image load, viewport resize. SSE
  items are a **strict subset** (only chunk-driven growth). The reflows that
  carry no SSE event and no `items` change — Prism finishing a highlight, an
  image loading, the final markdown finalize *after* `llm_done` when no more
  chunks come — would strand the viewport mid-content. Those were explicitly
  fixed bugs.
- To catch that growth without a ResizeObserver you need… a ResizeObserver (or a
  MutationObserver, which is worse). Not a simplification.
- `requestAnimationFrame`-after-commit can fire post-paint → visible
  paint-then-scroll flicker; the ResizeObserver callback runs pre-paint.

The SSE full-events stream (PR #532) was mistaken for a lever here. It changes
where events come from, not the DOM scroll mechanics — the observer is a strictly
more general growth signal than the event stream, so replacing one with the other
loses coverage.

## Consequences

- `sticky.ts` and its tests are unchanged — the mutation-tested controller stays
  the sole owner of the flag.
- Component-level scroll is still untestable in jsdom, so the new signal's
  contract is pinned in `store.test.ts` (switch bumps, send bumps,
  reloadSnapshot / prependOlder do NOT) rather than in a render test.
- `timeline.test.tsx` lost its `scrollToken === undefined` guard tests — the prop
  is gone; the pin is driven by the store signal.
