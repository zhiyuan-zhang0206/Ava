---
type: doc
title: Timeline sticky scroll
description: Sticky-bottom follow pins and scrollTop ownership (echo-attributed performed scrolls, wheel and touch zones, the pull reserve, the scroll-anchoring opt-out) plus the pinned work and detail headers, which stay pinned for the whole gesture.
tags:
- frontend
---

# Timeline sticky scroll

## Sticky bottom and scroll ownership
While following the tail, content growth pins the viewport down in the same
pre-paint tick — the ResizeObserver fires after layout and before paint — so
streamed text never paints below the fold and then scrolls into place, and
growth with no items change (throttled markdown/highlight flushes, async images)
rides the same signal. The follow flag has a single owner
(`lib/layout/sticky.ts:createStickyController`): the view wires DOM events in
(`handleScroll` / `handleWheel` / `handleLayoutChange`), performs the scrolls the
controller requests (`requestStick` / `pinToBottom`) and reports every performed
scroll back, which keeps the controller's baseline truthful and makes each pin's
echo a no-op. Wheel intent is an early release signal for fine pointers;
a scrollbar drag escapes through the controller's cumulative upward run instead;
touch devices fire no wheel. The bottom zone is pointer-dependent — touch keeps
the wide bounce-tolerant zone, mouse/trackpad a tight one so a small deliberate
scroll-up escapes — resolved once on mount, with the touch preset as the SSR
default. The viewport disables Chrome scroll anchoring (`overflow-anchor: none`):
a net-neutral reflow can move scrollTop on its own, a third hand the follow rule
cannot attribute; `overscroll-y-contain` keeps an exhausted vertical gesture out
of page-level scroll without touching scrollTop. A pending bookmark restore
lands before the pin, so the reader never sees the bottom first. The
scroll-to-bottom button reads the measured distance to the bottom rather than
the follow flag (a height change with no scroll event would otherwise leave it
hidden). Its smooth ride requests the stick instead of pinning, so the
asynchronous ride survives, and growth mid-ride pins instantly because the
controller is already following. A send or agent switch hides the button in the
same commit, supersedes an unreachable restore target, and pins with a fresh run
so a post-command twitch cannot release following while the awaited reply
streams in (user report 2026-09-19). Only a following reader releases the oldest
rows (the 250-row retention rule in the paging doc).

## Header scrolling scope
Human-message and agent-reply titles stay in normal flow, including historical,
streaming and interrupted replies. Expanded work blocks retain their sticky
header below HeaderBar. Their expanded detail cards retain a second sticky line
below the work header, using its measured `--turn-header-h`. Collapsed headers
stay in flow. The frosted pane and geometry tracking apply only to those work
and detail headers.

The pin is unconditional and speed-independent. It once carried a fast-scroll
escape — past ~1.8 px/ms the title dropped back into flow and re-pinned after a
settling delay — which meant a quick flick reliably lost the header of the
block being read while a slow drag looked correct (user report 2026-10-17).
`updateStuckHeader` therefore runs on every scroll frame regardless of speed,
so the frosted stuck variant stays truthful for the whole gesture; the release
also forced a second state source (a `data-fast-scrolling` flag on the
timeline wrapper) whose consumers are gone.

Event timestamps remain visible in message-card headers at narrow widths;
headers and timestamp text wrap when needed. Standing context notes continue
to omit timestamps according to their timeline item metadata.


```meiva-contract
id = "sticky-header-pin"
kind = "invariant"
statement = "An expanded work block's header, and its nested detail-card headers, stay pinned below HeaderBar for the whole gesture: no scroll speed, direction or settle timer releases them, so a pinned header's only state change is stuck versus resting presentation."
code = ["ui/web/src/components/timeline/card.tsx:STICKY_HEADER_CLS", "ui/web/src/components/timeline/card.tsx:STICKY_CHILD_HEADER_CLS"]
tests = ["ui/web/src/components/timeline/timeline.test.tsx::TimelineView sticky work block (task #2601)::keeps an expanded work block pinned through a rapid scroll burst", "ui/web/src/components/timeline/pinned-titles-autofollow.test.tsx::TimelineView pinned titles during auto-follow::keeps the pin when the reader leaves the bottom at speed"]
```

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]] — the renderer whose scroll surface and pinned headers this node owns.
